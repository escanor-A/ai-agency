import asyncio
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from check_stock import check_stock as db_check_stock
from check_stock import supabase
from orders import cancel_order, update_order_shipping, update_order_status
from subscriptions import (
    add_or_renew_subscription,
    check_and_update_subscriptions,
    get_tenant_subscription,
)

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
load_dotenv()

# مهم: مكتبات الشبكة ممكن تطبع رابط فيه التوكن لو المستوى INFO، فبنقفلها
logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-3.5-flash-lite"
 # غيّره لو الاسم اتغير في Google AI Studio

if "SUPER_ADMIN_CHAT_ID" not in os.environ or not os.environ["SUPER_ADMIN_CHAT_ID"].strip():
    raise ValueError("⚠️ المتغير SUPER_ADMIN_CHAT_ID إجباري ويجب ضبطه في ملف .env لحماية النظام!")
SUPER_ADMIN_CHAT_ID = int(os.environ["SUPER_ADMIN_CHAT_ID"].strip())

SYSTEM_PROMPT = """أنت مساعد مخزن لصاحب محل ملابس مصري، بتتكلم معاه على تليجرام.
أسلوبك: لهجة مصرية بسيطة، ردود قصيرة جداً.

قواعد:
1. لأي استعلام عن المخزون استخدم check_stock. ممنوع تخمّن كميات.
2. لو صاحب المحل قال إنه باع، أو وصلته بضاعة، أو في مرتجع: حدد الصنف بـ check_stock عشان تجيب كود الصنف (sku)، وبعدين نادِ propose_stock_change.
3. propose_stock_change مابتنفذش حاجة، هي بس بتجهّز العملية. الأزرار هي اللي هتخلي صاحب المحل يأكد. ماتقولش إن العملية اتنفذت أبداً.
4. لو المقاس أو اللون ناقص أو في أكتر من احتمال، اسأل بدل ما تخمّن.
5. لو الكمية ماتقالتش افترض 1.
6. أنواع العمليات: sale = بيع من المحل (بينقص)، restock = وصلت بضاعة (بتزيد)، return = مرتجع (بيزيد).
7. لو صاحب المحل طلب إضافة منتج جديد (مش موجود قبل كده في المخزن) واكتملت بياناته (الاسم، السعر، المقاس، اللون، الكمية): استدعي دالة propose_new_product فوراً في الخلفية! ممنوع تقول باللسان 'تأكد من الأزرار' أو 'جهزت الإضافة' من غير ما تستدعي الدالة فعلياً.
8. لو ذكر كذا لون أو مقاس لنفس المنتج، نادِ propose_new_product لكل لون/مقاس في نفس الرد فوراً لتجهيزهم معاً دفعة واحدة!
9. تذكّر سياق الرسائل السابقة واجمع البيانات الناقصة من كلام التاجر بدون ما تعيد السؤال عن حاجات قالها بالفعل.
10. لو صاحب المحل طلب إضافة فرع أو عنوان للمحل (مثلاً: فرع مدينة نصر، شارع كذا، لوكيشن ماب كذا)، استخرج البيانات ونادِ propose_new_branch.
11. لو صاحب المحل طلب عمل كود خصم أو عرض ترويجي (مثلاً: كود EID20 خصم 20%، أو عرض شحن مجاني لأي حد يطلب قطعتين أو أكتر)، استخرج البيانات ونادِ propose_new_promo.
12. لو صاحب المحل طلب إلغاء أوردر (مثلاً: الغي أوردر 5)، أو تعديل عنوان/موبايل أوردر، أو تحديث حالته (مثلاً: أوردر 5 تم الشحن / اتسلم): استدعِ أداة cancel_order_direct أو update_order_direct فوراً.

معلومات: المقاسات M و L و XL و 40 و 41 و 42 و 43 وغيرها (لارج = L، ميديم = M، إكس لارج = XL). الألوان بالعربي: أسود، أبيض، رمادي، كحلي، بني، بيج، أحمر، أصفر، بنفسجي، أخضر.
"""

NOT_OWNER_MESSAGE = "البوت ده خاص بصاحب المحل. لو إنت صاحب المحل، افتح رابط الربط اللي وصلك."


def get_owner_tenant(chat_id):
    """بيرجّع التاجر لو الشات ده مربوط بأي تاجر في السيستم، غير كده None"""
    rows = (
        supabase.table("tenants")
        .select("id,name")
        .eq("telegram_chat_id", chat_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


# سجل مؤقت لمحاولات الربط لحظر التخمين (Rate Limiting)
# {chat_id: {"failures": int, "blocked_until": float}}
LINK_CODE_ATTEMPTS = {}
MAX_FAILED_LINK_ATTEMPTS = 5
LINK_LOCKOUT_SECONDS = 15 * 60  # 15 دقيقة حظر بعد 5 محاولات فاشلة


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat_id = update.effective_chat.id
    code = context.args[0].strip().upper() if context.args else None

    if code:
        now = time.time()
        rate_info = LINK_CODE_ATTEMPTS.setdefault(chat_id, {"failures": 0, "blocked_until": 0})
        
        # 1. فحص إذا كان المستخدم محظوراً مؤقتاً بسبب تكرار المحاولات
        if rate_info["blocked_until"] > now:
            remaining_mins = int((rate_info["blocked_until"] - now) // 60) + 1
            await msg.reply_text(f"⛔ تم حظر المحاولات مؤقتاً لتكرار إدخال أكواد خاطئة. جرّب بعد {remaining_mins} دقيقة.")
            return

        def handle_failure(reason_text="الكود غلط أو اتستخدم قبل كده."):
            rate_info["failures"] += 1
            if rate_info["failures"] >= MAX_FAILED_LINK_ATTEMPTS:
                rate_info["blocked_until"] = now + LINK_LOCKOUT_SECONDS
                rate_info["failures"] = 0
                return "⛔ تم إدخال كود غير صالح 5 مرات. تم حظر محاولاتك لمدة 15 دقيقة لحماية الحسابات."
            else:
                remaining = MAX_FAILED_LINK_ATTEMPTS - rate_info["failures"]
                return f"{reason_text}\nباقي لك {remaining} محاولات قبل الحظر المؤقت."

        # 2. البحث عن الكود في كل التجار
        rows = (
            supabase.table("tenants")
            .select("id,name,telegram_link_code,created_at")
            .or_(f"telegram_link_code.eq.{code},telegram_link_code.ilike.{code}_%")
            .execute()
            .data
        )
        if not rows:
            await msg.reply_text(handle_failure())
            return
        
        target_tenant = rows[0]
        stored_code = target_tenant.get("telegram_link_code") or ""

        # 3. فحص صلاحية الكود (24 ساعة)
        is_expired = False
        if "_" in stored_code:
            _, _, expiry_str = stored_code.rpartition("_")
            try:
                expiry_ts = float(expiry_str)
                if now > expiry_ts:
                    is_expired = True
            except ValueError:
                pass
        elif target_tenant.get("created_at"):
            try:
                created_dt = datetime.fromisoformat(target_tenant["created_at"].replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - created_dt > timedelta(hours=24):
                    is_expired = True
            except Exception:
                pass

        if is_expired:
            supabase.table("tenants").update({"telegram_link_code": None}).eq("id", target_tenant["id"]).execute()
            await msg.reply_text(handle_failure("⛔ كود الربط انتهت صلاحيته (الصلاحية 24 ساعة فقط). اطلب من الأدمن توليد كود جديد."))
            return

        # الربط ناجح: مسح كود الربط وتصفير عداد المحاولات الفاشلة
        LINK_CODE_ATTEMPTS.pop(chat_id, None)
        supabase.table("tenants").update(
            {"telegram_chat_id": chat_id, "telegram_link_code": None}
        ).eq("id", target_tenant["id"]).execute()

        await msg.reply_text(
            f"تم ربط البوت بـ «{target_tenant['name']}» ✅\n"
            "هيوصلك هنا إشعار بكل أوردر جديد، وتقدر تدير مخزنك ومبيعاتك من هنا مباشرة.\n"
            "اكتب /help عشان تشوف تقدر تعمل إيه."
        )
        return

    tenant = get_owner_tenant(chat_id)
    if tenant:
        await msg.reply_text(f"أهلاً بيك في بوت «{tenant['name']}» 👋\nاكتب /help.")
    else:
        await msg.reply_text(NOT_OWNER_MESSAGE)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tenant = get_owner_tenant(chat_id)
    is_admin = (chat_id == SUPER_ADMIN_CHAT_ID)

    if not tenant and not is_admin:
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return

    text = ""
    if is_admin:
        text += (
            "👑 *لوحة تحكم مدير النظام (Super Admin):*\n"
            "• `/new_tenant <اسم المحل> [الموبايل] [1m|3m]` — إنشاء تاجر جديد وتحديد مدة اشتراكه\n"
            "• `/tenants` — عرض قائمة التجار وحالة اشتراكاتهم\n"
            "• `/renew <الرقم> [1m|3m|6m|1y]` — تجديد اشتراك تاجر وتفعيل البوت\n"
            "• `/pause_bot <الرقم>` — إيقاف بوت تاجر مؤقتاً (لو مدفعش الاشتراك)\n"
            "• `/resume_bot <الرقم>` — إعادة تشغيل بوت تاجر بعد ما يدفع\n"
            "• `/delete_tenant <الرقم>` — حذف تاجر وبياناته نهائياً\n"
            "• `/link_page <الرقم> fb=<id> token=<token>` — ربط قنوات السوشيال ميديا\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
        )

    text += (
        "📦 *إدارة المخزن والمبيعات:*\n"
        "تقدر تكلمني بالعامية المصرية عادي جداً، مثلاً:\n"
        "• *بعت 2 تيشيرت أسود L*\n"
        "• *وصل 20 هودي رمادي L*\n"
        "• *المتاح إيه من التيشيرت الأبيض؟*\n"
        "• *ضيف فرع جديد في مدينة نصر...*\n"
        "• *اعمل كود خصم EID20 خصم 20%*\n\n"
        "📊 *الأوامر السريعة:*\n"
        "• `/orders` — آخر 5 أوردرات وتفاصيلها\n"
        "• `/today` — تقرير ملخص مبيعات اليوم\n"
        "• `/cancel <رقم_الأوردر>` — إلغاء أوردر وإرجاع بضاعته للمخزن\n\n"
        "📁 *تحديث المخزن دفعة واحدة:*\n"
        "ابعت أي ملف Excel (.xlsx) أو CSV فيه بضاعتك، وهقرأه وأحدث المخزن فوراً!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tenant = get_owner_tenant(update.effective_chat.id)
    if not tenant:
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return
    if not context.args:
        await update.message.reply_text("اكتب رقم الأوردر بعد الأمر، مثلاً:\n/cancel 1")
        return

    order_num = context.args[0].strip().replace("#", "")
    res = cancel_order(tenant["id"], order_num)
    if res["status"] == "ok":
        await update.message.reply_text(f"تم إلغاء الأوردر #{order_num} ورجعت الكميات للمخزن بنجاح ✅")
    else:
        await update.message.reply_text(f"فشل الإلغاء: {res.get('reason', 'حصل خطأ')}")


async def new_tenant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر خاص بالـ Super Admin فقط لإنشاء تاجر جديد وتوليد كود ربط فوري"""
    chat_id = update.effective_chat.id
    if chat_id != SUPER_ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ عذراً، الأمر ده مخصص للـ Super Admin فقط.")
        return

    if not context.args:
        await update.message.reply_text(
            "اكتب اسم المحل بعد الأمر، مثلاً:\n"
            "`/new_tenant محل السعادة`\n"
            "أو مع رقم الموبايل:\n"
            "`/new_tenant محل السعادة 01012345678`",
            parse_mode="Markdown"
        )
        return

    # استخراج الاسم والموبايل ومدة الاشتراك إن وجدت
    import re
    full_text = " ".join(context.args)

    # استخراج مدة الاشتراك: 1m, 3m, 6m, 1y (الافتراضي 1m = شهر)
    months = 1
    dur_match = re.search(r"\b(1m|3m|6m|12m|1y|شهرين|سنة)\b", full_text, re.IGNORECASE)
    if dur_match:
        tag = dur_match.group(1).lower()
        if "3" in tag:
            months = 3
        elif "6" in tag:
            months = 6
        elif "12" in tag or "y" in tag or "سنة" in tag:
            months = 12
        elif "شهرين" in tag:
            months = 2
        full_text = re.sub(r"\b(1m|3m|6m|12m|1y|شهرين|سنة)\b", "", full_text, flags=re.IGNORECASE).strip()

    # البحث عن رقم موبايل مصري أو دولي في النص
    phone_match = re.search(r"(\+?20\d{10}|01[0125]\d{8})", full_text.replace(" ", ""))
    if phone_match:
        phone = phone_match.group(0)
        # مسح الرقم من النص للحصول على الاسم فقط
        name = re.sub(r"(\+?20[\s\d]{10,}|01[0125][\s\d]{8,})", "", full_text).strip()
    else:
        phone = None
        name = full_text.strip()

    msg = update.effective_message
    if not name:
        await msg.reply_text("لازم تكتب اسم المحل!")
        return

    import secrets
    raw_code = secrets.token_hex(8).upper()  # 16 حرف عشوائي آمن
    expiry_ts = int(time.time() + 24 * 3600)  # صلاحية 24 ساعة
    link_code = f"{raw_code}_{expiry_ts}"

    # فحص إذا كان المتجر مسجل بالفعل لمنع التكرار
    try:
        existing = (
            supabase.table("tenants")
            .select("id, name, telegram_chat_id, telegram_link_code")
            .ilike("name", name)
            .execute()
            .data
        )
        if existing:
            ex = existing[0]
            if ex.get("telegram_chat_id"):
                await msg.reply_text(
                    f"⚠️ *تنبيه:* متجر «{name}» مسجل ومربوط بالفعل في السيستم!\n"
                    f"شوف بياناته عبر `/tenants` أو جدد اشتراكه بـ `/renew {name} {months}m`",
                    parse_mode="Markdown"
                )
                return
            else:
                code = link_code
                supabase.table("tenants").update({"telegram_link_code": link_code}).eq("id", ex["id"]).execute()
                await msg.reply_text(
                    f"ℹ️ *المحل «{name}» متسجل مسبقاً وبانتظار التفعيل:*\n\n"
                    f"🔑 *كود الربط:* `{code}`\n\n"
                    f"انسخ الرسالة دي وابعتها للتاجر:\n"
                    f"👇👇👇\n"
                    f"أهلاً بحضرتك في سيستم إدارة المبيعات والمخزن 🚀\n"
                    f"ادخل على البوت: @ORIK_1_bot\n"
                    f"واكتب الكود ده لتفعيل حسابك:\n"
                    f"`/start {code}`",
                    parse_mode="Markdown"
                )
                return
    except Exception as e:
        print("Error checking existing tenant:", e)

    data = {
        "name": name,
        "bot_enabled": True,
        "telegram_link_code": link_code,
    }
    if phone:
        data["owner_phone"] = phone

    try:
        res = supabase.table("tenants").insert(data).execute()
        if not res.data:
            await msg.reply_text("❌ حصل خطأ أثناء حفظ التاجر في قاعدة البيانات.")
            return

        t = res.data[0]
        # تفعيل مدة الاشتراك في نظام الاشتراكات
        sub = add_or_renew_subscription(t["id"], months=months)
        ends_str = sub["ends_at"].strftime("%Y-%m-%d")

        await msg.reply_text(
            f"🎉 *تم إنشاء حساب التاجر بنجاح!*\n\n"
            f"🏢 *المحل:* `{t['name']}`\n"
            f"🔑 *كود الربط:* `{link_code}`\n"
            f"📅 *مدة الاشتراك:* {months} شهر (ساري حتى: `{ends_str}`)\n\n"
            f"انسخ الرسالة دي وابعتها للتاجر:\n"
            f"👇👇👇\n"
            f"أهلاً بحضرتك في سيستم إدارة المبيعات والمخزن 🚀\n"
            f"ادخل على البوت: @ORIK_1_bot\n"
            f"واكتب الكود ده لتفعيل حسابك:\n"
            f"`/start {link_code}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.reply_text(f"❌ حدث خطأ: {e}")


async def link_page_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    أمر خاص بالـ Super Admin لربط حسابات السوشيال ميديا بتاجر معين.
    الاستخدام:
      /link_page <tenant_id> fb=<page_id> token=<access_token>
      /link_page <tenant_id> ig=<instagram_account_id>
      /link_page <tenant_id> wa=<whatsapp_phone_number_id> wa_token=<token>
      /link_page <tenant_id> fb=<page_id> ig=<ig_id> wa=<wa_id>
    مثال:
      /link_page 3 fb=112233445566 token=EAABxx...
    """
    chat_id = update.effective_chat.id
    if chat_id != SUPER_ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ الأمر ده للـ Super Admin بس.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📋 *طريقة استخدام /link_page:*\n\n"
            "`/link_page <tenant_id> fb=<page_id>`\n"
            "`/link_page <tenant_id> ig=<instagram_id>`\n"
            "`/link_page <tenant_id> wa=<whatsapp_phone_number_id>`\n"
            "`/link_page <tenant_id> tiktok=<tiktok_account_id>`\n"
            "`/link_page <tenant_id> token=<fb_page_token>`\n"
            "`/link_page <tenant_id> wa_token=<wa_access_token>`\n"
            "`/link_page <tenant_id> tt_token=<tiktok_access_token>`\n\n"
            "ممكن تجمعهم في أمر واحد:\n"
            "`/link_page 3 fb=112233 ig=998877 wa=556644 tiktok=778899 token=EAABxx... tt_token=TKxx...`\n\n"
            "📌 *تعرف الـ tenant_id ازاي?*\n"
            "ابعت: `/tenants` تشوف كل التجار وأرقامهم.",
            parse_mode="Markdown"
        )
        return

    tenant_id_str = context.args[0].strip()
    tenant = resolve_tenant(tenant_id_str)
    if not tenant:
        await update.message.reply_text(f"❌ لم أجد تاجر بهذا الرقم أو الاسم: `{tenant_id_str}`\nاستخدم /tenants تشوف الأرقام الصح.", parse_mode="Markdown")
        return

    updates = {}

    # تحليل المعاملات
    for arg in context.args[1:]:
        if "=" not in arg:
            continue
        key, _, val = arg.partition("=")
        key = key.lower().strip()
        val = val.strip()
        if not val:
            continue
        if key == "fb":
            updates["facebook_page_id"] = val
        elif key == "ig":
            updates["instagram_account_id"] = val
        elif key == "wa":
            updates["whatsapp_phone_number_id"] = val
        elif key == "token":
            updates["facebook_page_token"] = val
        elif key == "wa_token":
            updates["whatsapp_access_token"] = val
        elif key == "tiktok":
            updates["tiktok_account_id"] = val
        elif key == "tt_token":
            updates["tiktok_access_token"] = val

    if not updates:
        await update.message.reply_text("⚠️ مش فاهم أي معاملات! استخدم fb= أو ig= أو wa= أو token=")
        return

    try:
        supabase.table("tenants").update(updates).eq("id", tenant["id"]).execute()
    except Exception as e:
        await update.message.reply_text(f"❌ فشل التحديث: {e}")
        return

    # بناء رسالة التأكيد
    lines = [f"✅ *تم ربط حسابات «{tenant['name']}» بنجاح!*\n"]
    field_names = {
        "facebook_page_id": "📘 فيسبوك Page ID",
        "instagram_account_id": "📸 إنستجرام Account ID",
        "whatsapp_phone_number_id": "🟢 واتساب Phone Number ID",
        "tiktok_account_id": "🎵 تيك توك Account ID",
        "facebook_page_token": "🔑 Facebook Page Token",
        "whatsapp_access_token": "🔑 WhatsApp Access Token",
        "tiktok_access_token": "🔑 TikTok Access Token",
    }
    for field, label in field_names.items():
        if field in updates:
            val = updates[field]
            # إخفاء معظم التوكن للأمان
            if "token" in field.lower() and len(val) > 10:
                display = val[:6] + "..." + val[-4:]
            else:
                display = val
            lines.append(f"{label}: `{display}`")

    lines.append(
        f"\n🔗 *دلوقتي أي رسالة جاية للـ Page/Account ده هتروح لـ AI بتاع «{tenant['name']}» تلقائياً!*"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def resolve_tenant(identifier: str):
    """
    بيبحث عن التاجر:
    1. بالرقم الترتيبي السهل (1, 2, 3...) حسب ترتيب /tenants
    2. بالـ UUID المباشر
    3. بالاسم أو جزء من الاسم
    """
    rows = (
        supabase.table("tenants")
        .select("id, name, bot_enabled, telegram_chat_id, facebook_page_id, instagram_account_id, whatsapp_phone_number_id, tiktok_account_id")
        .order("name")
        .execute()
        .data or []
    )
    if not rows:
        return None

    clean = identifier.strip()
    if clean.isdigit():
        idx = int(clean) - 1
        if 0 <= idx < len(rows):
            return rows[idx]

    # مطابقة UUID كامل
    for r in rows:
        if str(r.get("id", "")).lower() == clean.lower():
            return r

    # مطابقة الاسم
    for r in rows:
        if clean.lower() in str(r.get("name", "")).lower():
            return r

    return None


async def list_tenants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للـ Super Admin يشوف كل التجار وأرقامهم وحالتهم."""
    chat_id = update.effective_chat.id
    if chat_id != SUPER_ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ الأمر ده للـ Super Admin بس.")
        return

    rows = (
        supabase.table("tenants")
        .select("id,name,bot_enabled,facebook_page_id,instagram_account_id,whatsapp_phone_number_id,tiktok_account_id,telegram_chat_id")
        .order("name")
        .execute()
        .data
    )
    if not rows:
        await update.message.reply_text("مفيش تجار مسجلين لحد دلوقتي.")
        return

    lines = ["📋 *قائمة التجار المسجلين في السيستم:*\n"]
    for i, t in enumerate(rows, 1):
        is_active = t.get("bot_enabled", True)
        status_badge = "🟢 شغال" if is_active else "🔴 موقوف"

        sub = get_tenant_subscription(t["id"])
        if sub["has_subscription"]:
            if sub["is_active"]:
                if sub["days_left"] <= 3:
                    sub_str = f"🟡 باقي {sub['days_left']} يوم ({sub['ends_at_str']})"
                else:
                    sub_str = f"⏳ باقي {sub['days_left']} يوم ({sub['ends_at_str']})"
            else:
                sub_str = f"🔴 منتهي من {abs(sub['days_left'])} يوم"
        else:
            sub_str = "📅 غير محدد (جدده بـ /renew)"

        channels = []
        if t.get("facebook_page_id"):
            channels.append("📘 فيسبوك")
        if t.get("instagram_account_id"):
            channels.append("📸 إنستا")
        if t.get("whatsapp_phone_number_id"):
            channels.append("🟢 واتساب")
        if t.get("tiktok_account_id"):
            channels.append("🎵 تيك توك")
        if t.get("telegram_chat_id"):
            channels.append("✈️ تليجرام")

        ch_str = " | ".join(channels) if channels else "لا توجد قنوات مربوطة بعد"

        lines.append(
            f"*{i}.* *«{t['name']}»* — {status_badge}\n"
            f"   📅 الاشتراك: {sub_str}\n"
            f"   🔗 القنوات: {ch_str}\n"
            f"   🆔 كود: `{t['id'][:8]}...`\n"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚡ *التحكم السريع في التجار:*")
    lines.append("• تجديد اشتراك: `/renew <الرقم> [1m|3m|6m|1y]` (مثال: `/renew 1 3m`)")
    lines.append("• إيقاف مؤقت: `/pause_bot <الرقم>` (مثلاً: `/pause_bot 1`)")
    lines.append("• إعادة تشغيل: `/resume_bot <الرقم>` (مثلاً: `/resume_bot 1`)")
    lines.append("• حذف تاجر: `/delete_tenant <الرقم>` (مثلاً: `/delete_tenant 1`)")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def pause_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إيقاف البوت مؤقتاً للتاجر (لو مدفعش الاشتراك)"""
    chat_id = update.effective_chat.id
    if chat_id != SUPER_ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ الأمر ده للـ Super Admin بس.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ اكتب رقم التاجر من قائمة /tenants أو اسمه:\n"
            "مثال: `/pause_bot 1` أو `/pause_bot عمهم`",
            parse_mode="Markdown"
        )
        return

    target = " ".join(context.args).strip()
    tenant = resolve_tenant(target)
    if not tenant:
        await update.message.reply_text(f"❌ لم أجد تاجر بهذا الرقم أو الاسم: `{target}`\nاستخدم /tenants لمعرفة الأرقام.", parse_mode="Markdown")
        return

    try:
        supabase.table("tenants").update({"bot_enabled": False}).eq("id", tenant["id"]).execute()
        await update.message.reply_text(
            f"⏸️ *تم إيقاف البوت للتاجر «{tenant['name']}» مؤقتاً!* 🔴\n\n"
            "السيستم دلوقتي مش هيرد على أي رسايل أو كومنتات على صفحاته حتى يدفع الاشتراك.\n\n"
            f"لإعادة تشغيله بعد الدفع:\n`/resume_bot {tenant['name']}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ فشل إيقاف البوت: {e}")


async def resume_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إعادة تشغيل البوت للتاجر بعد ما يدفع"""
    chat_id = update.effective_chat.id
    if chat_id != SUPER_ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ الأمر ده للـ Super Admin بس.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ اكتب رقم التاجر من قائمة /tenants أو اسمه:\n"
            "مثال: `/resume_bot 1` أو `/resume_bot عمهم`",
            parse_mode="Markdown"
        )
        return

    target = " ".join(context.args).strip()
    tenant = resolve_tenant(target)
    if not tenant:
        await update.message.reply_text(f"❌ لم أجد تاجر بهذا الرقم أو الاسم: `{target}`\nاستخدم /tenants لمعرفة الأرقام.", parse_mode="Markdown")
        return

    try:
        supabase.table("tenants").update({"bot_enabled": True}).eq("id", tenant["id"]).execute()
        await update.message.reply_text(
            f"▶️ *تم إعادة تشغيل البوت للتاجر «{tenant['name']}» بنجاح!* 🟢\n\n"
            "البوت هيرجع يرد فوراً على كل الرسائل والكومنتات في كل القنوات المربوطة.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ فشل تشغيل البوت: {e}")


async def delete_tenant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف التاجر وبياناته من السيستم"""
    chat_id = update.effective_chat.id
    if chat_id != SUPER_ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ الأمر ده للـ Super Admin بس.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ اكتب رقم التاجر من قائمة /tenants أو اسمه:\n"
            "مثال: `/delete_tenant 1` أو `/delete_tenant عمهم`",
            parse_mode="Markdown"
        )
        return

    args = context.args
    is_confirmed = False
    if len(args) > 1 and args[-1].lower() in ["confirm", "تأكيد", "نعم", "yes"]:
        is_confirmed = True
        target = " ".join(args[:-1]).strip()
    else:
        target = " ".join(args).strip()

    tenant = resolve_tenant(target)
    if not tenant:
        await update.message.reply_text(f"❌ لم أجد تاجر بهذا الرقم أو الاسم: `{target}`\nاستخدم /tenants", parse_mode="Markdown")
        return

    if not is_confirmed:
        await update.message.reply_text(
            f"⚠️ *تحذير: هل أنت متأكد من حذف التاجر «{tenant['name']}» نهائياً؟*\n"
            f"ده هيحذف كل منتجاته ومخزنه وسجل أوردراته بالكامل ولا يمكن التراجع!\n\n"
            f"للتأكيد، اكتب:\n"
            f"`/delete_tenant {target} confirm`",
            parse_mode="Markdown"
        )
        return

    tid = tenant["id"]
    try:
        for table in ["order_items", "orders", "variants", "products", "branches", "tenant_promos"]:
            try:
                supabase.table(table).delete().eq("tenant_id", tid).execute()
            except Exception:
                pass
        supabase.table("tenants").delete().eq("id", tid).execute()
        await update.message.reply_text(f"🗑️ *تم حذف التاجر «{tenant['name']}» وبياناته من السيستم نهائياً.* ✅", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ فشل الحذف: {e}")


async def renew_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تجديد اشتراك تاجر: /renew <الرقم أو الاسم> [1m | 3m | 6m | 1y]"""
    chat_id = update.effective_chat.id
    if chat_id != SUPER_ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ الأمر ده للـ Super Admin بس.")
        return

    if not context.args:
        await update.message.reply_text(
            "📋 *طريقة تجديد الاشتراك:*\n"
            "`/renew <رقم أو اسم التاجر> [المدة]`\n\n"
            "أمثلة:\n"
            "• `/renew 1 1m` — تجديد شهر للتاجر رقم 1\n"
            "• `/renew 1 3m` — تجديد 3 شهور للتاجر رقم 1\n"
            "• `/renew عمهم 3m` — تجديد 3 شهور لـ عمهم",
            parse_mode="Markdown"
        )
        return

    target = context.args[0].strip()
    months = 1
    if len(context.args) > 1:
        m_str = context.args[1].lower().strip()
        if "3" in m_str:
            months = 3
        elif "6" in m_str:
            months = 6
        elif "12" in m_str or "y" in m_str or "سنة" in m_str:
            months = 12
        elif "شهرين" in m_str:
            months = 2

    tenant = resolve_tenant(target)
    if not tenant:
        await update.message.reply_text(f"❌ لم أجد تاجر بهذا الرقم أو الاسم: `{target}`\nاستخدم /tenants", parse_mode="Markdown")
        return

    tid = tenant["id"]
    res = add_or_renew_subscription(tid, months=months)
    supabase.table("tenants").update({"bot_enabled": True}).eq("id", tid).execute()

    ends_str = res["ends_at"].strftime("%Y-%m-%d")
    await update.message.reply_text(
        f"✅ *تم تجديد اشتراك «{tenant['name']}» بنجاح!* 🎉\n\n"
        f"📅 *مدة التجديد:* {months} شهر\n"
        f"⏳ *تاريخ الانتهاء الجديد:* `{ends_str}` (باقي {res['days_left']} يوم)\n"
        f"🟢 *حالة البوت:* شغال ويرد تلقائياً على كل الرسائل.",
        parse_mode="Markdown"
    )

    tg_chat = tenant.get("telegram_chat_id")
    if tg_chat:
        try:
            await context.bot.send_message(
                chat_id=tg_chat,
                text=(
                    f"🎉 *مبروك! تم تجديد اشتراك متجرك «{tenant['name']}» بنجاح!*\n\n"
                    f"📅 *مدة التجديد:* {months} شهر\n"
                    f"⏳ *ساري حتى:* `{ends_str}`\n\n"
                    "خدمة الرد التلقائي وإدارة الأوردرات والمخزن تعمل بكامل طاقتها 🚀"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Failed to notify tenant {tenant['name']} of renewal:", e)


async def check_subscriptions_and_notify(app):
    """فحص دوري لكل اشتراكات التجار وإرسال التنبيهات للأدمن والتاجر"""
    alerts = check_and_update_subscriptions()

    # 1. تنبيهات اقتراب الانتهاء (3 أيام أو أقل)
    for w in alerts.get("warnings", []):
        tid = w["tenant_id"]
        rows = supabase.table("tenants").select("id, name, telegram_chat_id").eq("id", tid).execute().data
        if not rows:
            continue
        tenant = rows[0]
        days = w["days_left"]
        ends_str = w["ends_at"].strftime("%Y-%m-%d")

        try:
            await app.bot.send_message(
                chat_id=SUPER_ADMIN_CHAT_ID,
                text=(
                    f"🔔 *تنبيه تجديد اشتراك:* ⏳\n\n"
                    f"المحل: *«{tenant['name']}»*\n"
                    f"متبقي على انتهاء الاشتراك: *{days} أيام* (ينتهي في: `{ends_str}`)\n"
                    f"تواصل مع التاجر لتجديد الاشتراك.\n\n"
                    f"للتجديد السريع:\n`/renew {tenant['name']} 1m` أو `3m`"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print("Failed to alert admin:", e)

        if tenant.get("telegram_chat_id"):
            try:
                await app.bot.send_message(
                    chat_id=tenant["telegram_chat_id"],
                    text=(
                        f"⚠️ *تنبيه هام بخصوص اشتراكك:*\n\n"
                        f"عزيزي صاحب متجر «{tenant['name']}»، متبقي *{days} أيام فقط* على انتهاء فترة اشتراكك في السيستم (ينتهي في: `{ends_str}`).\n\n"
                        "يرجى التواصل مع إدارة السيستم لتجديد الاشتراك وتجنب توقف خدمة الرد التلقائي على العملاء 🙏"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print("Failed to alert tenant:", e)

    # 2. انتهاء الاشتراك الفعلي (إيقاف البوت فوراً)
    for exp in alerts.get("expirations", []):
        tid = exp["tenant_id"]
        rows = supabase.table("tenants").select("id, name, telegram_chat_id").eq("id", tid).execute().data
        if not rows:
            continue
        tenant = rows[0]
        ends_str = exp["ends_at"].strftime("%Y-%m-%d")

        supabase.table("tenants").update({"bot_enabled": False}).eq("id", tid).execute()

        try:
            await app.bot.send_message(
                chat_id=SUPER_ADMIN_CHAT_ID,
                text=(
                    f"🔴 *انتهاء اشتراك وإيقاف البوت:* ⏸️\n\n"
                    f"المحل: *«{tenant['name']}»*\n"
                    f"انتهى الاشتراك بتاريخ `{ends_str}` وتم إيقاف الرد الآلي للتاجر تلقائياً.\n\n"
                    f"أول ما التاجر يدفع ويجدد، اكتب:\n"
                    f"`/renew {tenant['name']} 1m` أو `3m`"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print("Failed to alert admin on expiration:", e)

        if tenant.get("telegram_chat_id"):
            try:
                await app.bot.send_message(
                    chat_id=tenant["telegram_chat_id"],
                    text=(
                        f"🔴 *تنبيه: انتهت فترة اشتراك متجرك «{tenant['name']}»!*\n\n"
                        "تم إيقاف خدمة الرد التلقائي وإدارة المبيعات مؤقتاً.\n"
                        "يرجى التواصل مع الإدارة وسداد الاشتراك لإعادة تفعيل الخدمة فوراً 🚀"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print("Failed to alert tenant on expiration:", e)


async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tenant = get_owner_tenant(update.effective_chat.id)
    if not tenant:
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return
    rows = (
        supabase.table("orders")
        .select("order_number,customer_name,governorate,total,status")
        .eq("tenant_id", tenant["id"])
        .order("created_at", desc=True)
        .limit(5)
        .execute()
        .data
    )
    if not rows:
        await update.message.reply_text("لسه مفيش أوردرات.")
        return
    lines = [
        f"#{r['order_number']} | {r['customer_name']} | {r['governorate']} | {float(r['total']):.0f} جنيه | {r['status']}"
        for r in rows
    ]
    await update.message.reply_text("آخر الأوردرات:\n" + "\n".join(lines))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tenant = get_owner_tenant(update.effective_chat.id)
    if not tenant:
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = (
        supabase.table("orders")
        .select("total,status")
        .eq("tenant_id", tenant["id"])
        .gte("created_at", since)
        .execute()
        .data
    )
    valid = [r for r in rows if r["status"] != "cancelled"]
    revenue = sum(float(r["total"]) for r in valid)
    await update.message.reply_text(
        f"آخر 24 ساعة:\n🛒 أوردرات من البوت: {len(valid)}\n💰 إجمالي: {revenue:.0f} جنيه"
    )


# جلسات محادثات التليجرام لحفظ الذاكرة وسياق الكلام
MERCHANT_SESSIONS = {}
MAX_MERCHANT_SESSIONS = 200
MERCHANT_SESSION_TTL = 4 * 3600  # 4 ساعات صلاحية الجلسة بالرام


def run_llm(chat_id: int, content, tools: list):
    """بيشغّل الموديل مع الأدوات وسياق المحادثة المحفوظ مع إدارة الذاكرة ومنع تسريب الرام"""
    now = time.time()
    config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, tools=tools)

    # تنظيف الجلسات القديمة لمنع تضخم الرام (LRU / TTL)
    if len(MERCHANT_SESSIONS) > MAX_MERCHANT_SESSIONS:
        expired = [cid for cid, sess in MERCHANT_SESSIONS.items() if now - sess.get("last_active", 0) > MERCHANT_SESSION_TTL]
        for cid in expired:
            MERCHANT_SESSIONS.pop(cid, None)
        if len(MERCHANT_SESSIONS) > MAX_MERCHANT_SESSIONS:
            sorted_s = sorted(MERCHANT_SESSIONS.keys(), key=lambda k: MERCHANT_SESSIONS[k].get("last_active", 0))
            for k in sorted_s[:MAX_MERCHANT_SESSIONS // 5]:
                MERCHANT_SESSIONS.pop(k, None)

    if chat_id not in MERCHANT_SESSIONS:
        chat = client.chats.create(model=MODEL, config=config)
        MERCHANT_SESSIONS[chat_id] = {"chat": chat, "last_active": now}
    else:
        MERCHANT_SESSIONS[chat_id]["last_active"] = now
        chat = MERCHANT_SESSIONS[chat_id]["chat"]
        chat._config = config

    for attempt in range(3):
        try:
            return chat.send_message(content).text or "تمام، أقدر أساعدك في إيه تاني؟"
        except Exception as e:
            msg = str(e)
            if "429" in msg:
                print("وصلنا لحد الحصة المجانية:", msg[:300])
                return "وصلنا لحد الاستخدام المجاني للذكاء الاصطناعي دلوقتي، جرّب بعد شوية."
            if "503" in msg and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print("خطأ من الموديل:", type(e).__name__, msg[:300])
            MERCHANT_SESSIONS.pop(chat_id, None)
            return "حصلت مشكلة مؤقتة، جرّب تاني بعد شوية."

MAX_PENDING_OPERATIONS = 500
PENDING_OPERATION_TTL = 7200  # ساعتان كحد أقصى لتأكيد العملية في الرام


def _get_bot_data(context):
    if hasattr(context, "application") and hasattr(context.application, "bot_data"):
        return context.application.bot_data
    if hasattr(context, "bot_data"):
        return context.bot_data
    if isinstance(context, dict):
        return context
    return {}


def store_pending_operation(context, pid: str, data: dict):
    """حفظ عملية معلقة في الذاكرة مع وقت إنشاء وتنظيف العمليات القديمة لمنع تضخم الرام"""
    now = time.time()
    bot_data = _get_bot_data(context)
    pending = bot_data.setdefault("pending", {})
    if len(pending) > MAX_PENDING_OPERATIONS:
        expired = [k for k, v in pending.items() if now - v.get("created_at", 0) > PENDING_OPERATION_TTL]
        for k in expired:
            pending.pop(k, None)
        if len(pending) > MAX_PENDING_OPERATIONS:
            sorted_keys = sorted(pending.keys(), key=lambda k: pending[k].get("created_at", 0))
            for k in sorted_keys[:MAX_PENDING_OPERATIONS // 5]:
                pending.pop(k, None)
    data["created_at"] = now
    pending[pid] = data


def pop_pending_operation(context, pid: str) -> dict:
    """استرجاع العملية المعلقة وحذفها مع فحص انتهاء صلاحيتها (ساعتان)"""
    bot_data = _get_bot_data(context)
    pending = bot_data.get("pending", {}).pop(pid, None)
    if not pending:
        return None
    if time.time() - pending.get("created_at", 0) > PENDING_OPERATION_TTL:
        return None
    return pending


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tenant = get_owner_tenant(chat_id)
    if not tenant:
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return

    tenant_id = tenant["id"]
    proposals = []  # العمليات اللي الموديل جهّزها وتستنى التأكيد

    def check_stock(product_name: str, size: str = "", color: str = "") -> dict:
        """بتكشف المخزون لمنتج بمقاس ولون معين، وبترجّع كود الصنف (sku) والكمية المتاحة.

        Args:
            product_name: اسم المنتج أو جزء منه (تيشيرت، هودي، بنطلون)
            size: المقاس M أو L أو XL (فاضي لو ماتحددش)
            color: اللون بالعربي (فاضي لو ماتحددش)
        """
        return db_check_stock(
            tenant_id, product_name, size=size or None, color=color or None
        )

    def propose_stock_change(sku: str, qty: int, kind: str) -> dict:
        """بتجهّز تعديل على المخزون وتستنى تأكيد صاحب المحل بالأزرار. مابتنفذش حاجة.

        Args:
            sku: كود الصنف اللي جاي من check_stock
            qty: الكمية (رقم موجب)
            kind: نوع العملية: sale (بيع من المحل) أو restock (وصلت بضاعة) أو return (مرتجع)
        """
        if qty <= 0:
            return {"status": "error", "reason": "الكمية لازم تكون أكبر من صفر"}
        if kind not in ("sale", "restock", "return"):
            return {"status": "error", "reason": "نوع العملية غلط"}
        rows = (
            supabase.table("variants")
            .select("id,sku,size,color,available,products!inner(name)")
            .eq("tenant_id", tenant_id)
            .eq("sku", sku)
            .execute()
            .data
        )
        if not rows:
            return {"status": "error", "reason": f"الكود {sku} مش موجود"}
        v = rows[0]
        if kind == "sale" and v["available"] < qty:
            return {"status": "error", "reason": f"المتاح {v['available']} بس"}
        label = f"{v['products']['name']} {v['color']} {v['size']}"
        change = -qty if kind == "sale" else qty
        proposals.append(
            {"type": "movement", "variant_id": v["id"], "label": label, "change": change, "kind": kind}
        )
        return {"status": "waiting_confirmation", "item": label, "change": change}

    def propose_new_product(name: str, price: float, size: str, color: str, qty: int = 1) -> dict:
        """بتجهّز إضافة منتج أو صنف جديد خالص في المخزن وتستنى تأكيد صاحب المحل.
        
        Args:
            name: اسم المنتج (مثلاً: جاكيت جينز، سويت شيرت)
            price: سعر البيع بالجنيه
            size: المقاس (M, L, XL, 42, إلخ)
            color: اللون بالعربي (أسود، كحلي، إلخ)
            qty: الكمية المتاحة الأولية (افتراضي 1)
        """
        if price <= 0:
            return {"status": "error", "reason": "السعر لازم يكون أكبر من صفر"}
        if qty <= 0:
            return {"status": "error", "reason": "الكمية لازم تكون أكبر من صفر"}
        
        proposals.append({
            "type": "new_product",
            "name": name.strip(),
            "price": float(price),
            "size": size.strip().upper(),
            "color": color.strip(),
            "qty": int(qty),
        })
        return {"status": "waiting_confirmation", "product": name, "price": price}

    def propose_new_branch(name: str, governorate: str, address: str, google_maps_url: str = "", working_hours: str = "من 12 ظهراً لـ 11 مساءً") -> dict:
        """بتجهّز إضافة فرع أو عنوان للمحل وتستنى تأكيد صاحب المحل.
        
        Args:
            name: اسم الفرع (مثلاً: فرع مدينة نصر، فرع الدقي)
            governorate: المحافظة (القاهرة، الجيزة، الإسكندرية...)
            address: العنوان بالتفصيل
            google_maps_url: رابط اللوكيشن على خرائط جوجل (إن وجد)
            working_hours: مواعيد العمل
        """
        proposals.append({
            "type": "new_branch",
            "name": name.strip(),
            "governorate": governorate.strip(),
            "address": address.strip(),
            "google_maps_url": google_maps_url.strip(),
            "working_hours": working_hours.strip(),
        })
        return {"status": "waiting_confirmation", "branch": name}

    def propose_new_promo(code: str = "", promo_type: str = "percentage", discount_value: float = 0.0, description: str = "", min_order_items: int = 1) -> dict:
        """بتجهّز إضافة كود خصم أو عرض ترويجي للمحل (شحن مجاني، نسبة خصم، أو خصم بالجنيه) وتستنى تأكيد التاجر.
        
        Args:
            code: كود الخصم (زي EID20 أو SUMMER10) أو اتركه فارغاً لو عرض عام تلقائي (زي شحن مجاني لأي حد يطلب قطعتين)
            promo_type: نوع الخصم (percentage = نسبة مئوية، fixed = خصم بالجنيه، free_shipping = شحن مجاني)
            discount_value: قيمة الخصم (مثلاً 20 لو 20% أو 50 لو 50 جنيه)
            description: وصف العرض بالعربي
            min_order_items: الحد الأدنى لعدد القطع لتطبيق العرض (افتراضي 1)
        """
        proposals.append({
            "type": "new_promo",
            "code": code.strip().upper(),
            "promo_type": promo_type.strip(),
            "discount_value": float(discount_value),
            "description": description.strip() or f"عرض {code}",
            "min_order_items": int(min_order_items),
        })
        return {"status": "waiting_confirmation", "promo": description}

    def cancel_order_direct(order_number: int, reason: str = "") -> dict:
        """إلغاء أوردر فوراً وإرجاع بضاعته للمخزن."""
        return cancel_order(tenant_id, order_number, reason=reason)

    def update_order_direct(order_number: int, phone: str = "", address: str = "", status: str = "") -> dict:
        """تحديث عنوان، رقم موبايل، أو حالة أوردر (shipped / delivered)."""
        res = {}
        if phone or address:
            res["shipping"] = update_order_shipping(tenant_id, order_number, phone=phone, address=address)
        if status:
            res["status"] = update_order_status(tenant_id, order_number, new_status=status)
        return res or {"status": "ok"}

    content_to_send = update.message.text
    reply = await asyncio.to_thread(
        run_llm, chat_id, content_to_send, [
            check_stock, propose_stock_change, propose_new_product,
            propose_new_branch, propose_new_promo, cancel_order_direct, update_order_direct
        ]
    )

    if not proposals:
        await update.message.reply_text(reply)
        return

    pid = uuid.uuid4().hex[:12]
    store_pending_operation(context, pid, {
        "chat_id": chat_id,
        "tenant_id": tenant_id,
        "items": proposals,
    })
    kinds = {"sale": "خصم (بيع من المحل)", "restock": "إضافة بضاعة", "return": "مرتجع"}
    
    summary_lines = []
    for p in proposals:
        if p.get("type") == "new_product":
            summary_lines.append(f"✨ إضافة منتج جديد: {p['name']} ({p['color']} - {p['size']}) | السعر: {p['price']:.0f} جنيه | الكمية: {p['qty']}")
        elif p.get("type") == "new_branch":
            summary_lines.append(f"📍 إضافة فرع جديد: {p['name']} ({p['governorate']} - {p['address']})")
        elif p.get("type") == "new_promo":
            code_txt = f" (كود: {p['code']})" if p.get('code') else ""
            summary_lines.append(f"🎁 إضافة عرض/خصم: {p['description']}{code_txt}")
        else:
            summary_lines.append(f"• {kinds[p['kind']]}: {abs(p['change'])} × {p['label']}")

    summary = "\n".join(summary_lines)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأكيد", callback_data=f"ok:{pid}"),
                InlineKeyboardButton("❌ إلغاء", callback_data=f"no:{pid}"),
            ]
        ]
    )
    await update.message.reply_text(
        f"هنفّذ العملية دي:\n{summary}\n\nتأكيد؟", reply_markup=keyboard
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال وتحليل الفويسات الصوتية مباشرة بـ Gemini Multimodal"""
    chat_id = update.effective_chat.id
    tenant = get_owner_tenant(chat_id)
    if not tenant:
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return

    tenant_id = tenant["id"]
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    status_msg = await update.message.reply_text("🎙️ جاري الاستماع للفويس وتحليله...")

    # تحميل ملف الصوت من تليجرام في الذاكرة
    voice_file = await context.bot.get_file(voice.file_id)
    audio_bytes = await voice_file.download_as_bytearray()

    proposals = []

    def check_stock(product_name: str, size: str = "", color: str = "") -> dict:
        return db_check_stock(tenant_id, product_name, size=size or None, color=color or None)

    def propose_stock_change(sku: str, qty: int, kind: str) -> dict:
        if qty <= 0:
            return {"status": "error", "reason": "الكمية لازم تكون أكبر من صفر"}
        if kind not in ("sale", "restock", "return"):
            return {"status": "error", "reason": "نوع العملية غلط"}
        rows = (
            supabase.table("variants")
            .select("id,sku,size,color,available,products!inner(name)")
            .eq("tenant_id", tenant_id)
            .eq("sku", sku)
            .execute()
            .data
        )
        if not rows:
            return {"status": "error", "reason": f"الكود {sku} مش موجود"}
        v = rows[0]
        if kind == "sale" and v["available"] < qty:
            return {"status": "error", "reason": f"المتاح {v['available']} بس"}
        label = f"{v['products']['name']} {v['color']} {v['size']}"
        change = -qty if kind == "sale" else qty
        proposals.append(
            {"type": "movement", "variant_id": v["id"], "label": label, "change": change, "kind": kind}
        )
        return {"status": "waiting_confirmation", "item": label, "change": change}

    def propose_new_product(name: str, price: float, size: str, color: str, qty: int = 1) -> dict:
        if price <= 0 or qty <= 0:
            return {"status": "error", "reason": "السعر والكمية لازم يكونوا أكبر من صفر"}
        proposals.append({
            "type": "new_product",
            "name": name.strip(),
            "price": float(price),
            "size": size.strip().upper(),
            "color": color.strip(),
            "qty": int(qty),
        })
        return {"status": "waiting_confirmation", "product": name, "price": price}

    def propose_new_branch(name: str, governorate: str, address: str, google_maps_url: str = "", working_hours: str = "من 12 ظهراً لـ 11 مساءً") -> dict:
        proposals.append({
            "type": "new_branch",
            "name": name.strip(),
            "governorate": governorate.strip(),
            "address": address.strip(),
            "google_maps_url": google_maps_url.strip(),
            "working_hours": working_hours.strip(),
        })
        return {"status": "waiting_confirmation", "branch": name}

    def propose_new_promo(code: str = "", promo_type: str = "percentage", discount_value: float = 0.0, description: str = "", min_order_items: int = 1) -> dict:
        proposals.append({
            "type": "new_promo",
            "code": code.strip().upper(),
            "promo_type": promo_type.strip(),
            "discount_value": float(discount_value),
            "description": description.strip() or f"عرض {code}",
            "min_order_items": int(min_order_items),
        })
        return {"status": "waiting_confirmation", "promo": description}

    def cancel_order_direct(order_number: int, reason: str = "") -> dict:
        """إلغاء أوردر فوراً وإرجاع بضاعته للمخزن."""
        return cancel_order(tenant_id, order_number, reason=reason)

    def update_order_direct(order_number: int, phone: str = "", address: str = "", status: str = "") -> dict:
        """تحديث عنوان، رقم موبايل، أو حالة أوردر (shipped / delivered)."""
        res = {}
        if phone or address:
            res["shipping"] = update_order_shipping(tenant_id, order_number, phone=phone, address=address)
        if status:
            res["status"] = update_order_status(tenant_id, order_number, new_status=status)
        return res or {"status": "ok"}

    # تمرير الصوت كـ Part إلى Gemini
    audio_part = types.Part.from_bytes(data=bytes(audio_bytes), mime_type="audio/ogg")
    content_list = [audio_part, "استمع للريكورد ده من صاحب المحل ونفّذ المطلوب واستدعِ الأدوات المناسبة."]

    reply = await asyncio.to_thread(
        run_llm, chat_id, content_list, [
            check_stock, propose_stock_change, propose_new_product,
            propose_new_branch, propose_new_promo, cancel_order_direct, update_order_direct
        ]
    )

    await status_msg.delete()

    if not proposals:
        await update.message.reply_text(reply)
        return

    pid = uuid.uuid4().hex[:12]
    store_pending_operation(context, pid, {
        "chat_id": chat_id,
        "tenant_id": tenant_id,
        "items": proposals,
    })
    kinds = {"sale": "خصم (بيع من المحل)", "restock": "إضافة بضاعة", "return": "مرتجع"}
    summary_lines = []
    for p in proposals:
        if p.get("type") == "new_product":
            summary_lines.append(f"✨ إضافة منتج جديد: {p['name']} ({p['color']} - {p['size']}) | السعر: {p['price']:.0f} جنيه | الكمية: {p['qty']}")
        elif p.get("type") == "new_branch":
            summary_lines.append(f"📍 إضافة فرع جديد: {p['name']} ({p['governorate']} - {p['address']})")
        elif p.get("type") == "new_promo":
            code_txt = f" (كود: {p['code']})" if p.get('code') else ""
            summary_lines.append(f"🎁 إضافة عرض/خصم: {p['description']}{code_txt}")
        else:
            summary_lines.append(f"• {kinds[p['kind']]}: {abs(p['change'])} × {p['label']}")

    summary = "\n".join(summary_lines)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأكيد", callback_data=f"ok:{pid}"),
                InlineKeyboardButton("❌ إلغاء", callback_data=f"no:{pid}"),
            ]
        ]
    )
    await update.message.reply_text(
        f"سمعت الفويس، وهنفّذ العملية دي:\n{summary}\n\nتأكيد؟", reply_markup=keyboard
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, _, pid = query.data.partition(":")
    chat_id = query.message.chat.id

    tenant = get_owner_tenant(chat_id)
    # pop = العملية تتنفذ مرة واحدة بس حتى لو الزرار اتداس مرتين، مع التحقق من انتهاء الصلاحية
    pending = pop_pending_operation(context, pid)
    if not pending or pending["chat_id"] != chat_id or not tenant:
        await query.edit_message_text("العملية دي انتهت صلاحيتها أو اتنفذت قبل كده.")
        return

    tenant_id = pending.get("tenant_id") or tenant["id"]

    if action == "no":
        await query.edit_message_text("اتلغت ❌")
        return

    failures = []
    success_new_products = []
    success_branches = []
    success_promos = []
    for i, item in enumerate(pending["items"]):
        if item.get("type") == "new_promo":
            try:
                supabase.table("promotions").insert({
                    "tenant_id": tenant_id,
                    "code": item.get("code") or None,
                    "promo_type": item["promo_type"],
                    "discount_value": item.get("discount_value", 0),
                    "min_order_items": item.get("min_order_items", 1),
                    "description": item["description"],
                    "is_active": True,
                }).execute()
                success_promos.append(item["description"])
            except Exception as e:
                print("فشل إضافة العرض:", e)
                failures.append(item["description"])
        elif item.get("type") == "new_branch":
            try:
                supabase.table("branches").insert({
                    "tenant_id": tenant_id,
                    "name": item["name"],
                    "governorate": item["governorate"],
                    "address": item["address"],
                    "google_maps_url": item.get("google_maps_url"),
                    "working_hours": item.get("working_hours", "من 12 ظهراً لـ 11 مساءً"),
                }).execute()
                success_branches.append(f"{item['name']} ({item['address']})")
            except Exception as e:
                print("فشل إضافة الفرع:", e)
                failures.append(item["name"])
        elif item.get("type") == "new_product":
            try:
                # 1. فحص هل المنتج نفسه موجود، لو مش موجود ننشئه
                prod_rows = (
                    supabase.table("products")
                    .select("id")
                    .eq("tenant_id", tenant_id)
                    .ilike("name", item["name"])
                    .execute()
                    .data
                )
                if prod_rows:
                    product_id = prod_rows[0]["id"]
                else:
                    new_p = (
                        supabase.table("products")
                        .insert({
                            "tenant_id": tenant_id,
                            "name": item["name"],
                            "price": item["price"],
                            "active": True,
                        })
                        .execute()
                        .data
                    )
                    product_id = new_p[0]["id"]

                # 2. توليد sku للصنف
                sku_clean = "".join(w[0] for w in item["name"].split()[:2]).upper()
                sku = f"{sku_clean}-{item['size']}-{uuid.uuid4().hex[:4].upper()}"

                # 3. إنشاء الصنف variant
                supabase.table("variants").insert({
                    "tenant_id": tenant_id,
                    "product_id": product_id,
                    "sku": sku,
                    "size": item["size"],
                    "color": item["color"],
                    "stock": item["qty"],
                    "reserved": 0,
                    "active": True,
                }).execute()
                success_new_products.append(f"{item['name']} ({item['color']} {item['size']})")
            except Exception as e:
                print("فشل إضافة المنتج الجديد:", e)
                failures.append(item["name"])
        else:
            try:
                supabase.rpc(
                    "apply_stock_movement",
                    {
                        "p_tenant": tenant_id,
                        "p_variant": item["variant_id"],
                        "p_change": item["change"],
                        "p_reason": item["kind"],
                        "p_source": "telegram",
                        "p_ref": f"tg-{pid}-{i}",
                    },
                ).execute()
            except Exception as e:
                print("فشل تعديل المخزون:", type(e).__name__)
                failures.append(item["label"])

    if failures:
        await query.edit_message_text("حصلت مشكلة في: " + "، ".join(failures))
    elif success_promos:
        await query.edit_message_text(f"تم تفعيل العرض بنجاح 🎁✅:\n" + "\n".join(f"• {p}" for p in success_promos))
    elif success_branches:
        await query.edit_message_text(f"تم إضافة الفرع بنجاح 📍✅:\n" + "\n".join(f"• {b}" for b in success_branches))
    elif success_new_products:
        await query.edit_message_text(f"تم إضافة المنتج بنجاح ✅:\n" + "\n".join(f"• {p}" for p in success_new_products))
    else:
        await query.edit_message_text("تم ✅ المخزون اتحدّث.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    استقبال ملف Excel أو CSV من التاجر وتحليله وإضافة المنتجات تلقائياً.
    الأعمدة المتوقعة: اسم المنتج | السعر | المقاس | اللون | الكمية
    """
    chat_id = update.effective_chat.id
    tenant = get_owner_tenant(chat_id)
    if not tenant:
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return

    doc = update.message.document
    if not doc:
        return

    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".xlsx") or fname.endswith(".xls") or fname.endswith(".csv")):
        await update.message.reply_text(
            "📋 ابعتلي ملف Excel (.xlsx) أو CSV (.csv)\n"
            "الأعمدة المطلوبة:\n"
            "*اسم المنتج | السعر | المقاس | اللون | الكمية*",
            parse_mode="Markdown"
        )
        return

    status_msg = await update.message.reply_text("📊 جاري قراءة الشيت...")

    # تحميل الملف
    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()

    rows_parsed = []
    errors = []

    try:
        if fname.endswith(".csv"):
            import csv, io
            text = file_bytes.decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            raw_rows = list(reader)
        else:
            import openpyxl, io
            wb = openpyxl.load_workbook(io.BytesIO(bytes(file_bytes)), data_only=True)
            ws = wb.active
            headers = [str(c.value or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
            raw_rows = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                raw_rows.append(dict(zip(headers, [str(v or "").strip() for v in row])))
    except Exception as e:
        await status_msg.edit_text(f"❌ فشل قراءة الملف: {type(e).__name__}")
        return

    # تطبيع أسماء الأعمدة (بيقبل عربي وإنجليزي)
    COL_MAP = {
        "name":  ["اسم المنتج", "المنتج", "الاسم", "name", "product"],
        "price": ["السعر", "سعر البيع", "price"],
        "size":  ["المقاس", "مقاس", "size"],
        "color": ["اللون", "لون", "color"],
        "qty":   ["الكمية", "كمية", "qty", "quantity", "stock"],
    }

    def find_col(row_keys, aliases):
        for k in row_keys:
            if k in aliases:
                return k
        return None

    if not raw_rows:
        await status_msg.edit_text("❌ الملف فاضي!")
        return

    first_keys = list(raw_rows[0].keys())
    col_name  = find_col(first_keys, COL_MAP["name"])
    col_price = find_col(first_keys, COL_MAP["price"])
    col_size  = find_col(first_keys, COL_MAP["size"])
    col_color = find_col(first_keys, COL_MAP["color"])
    col_qty   = find_col(first_keys, COL_MAP["qty"])

    missing = [k for k, v in {"اسم المنتج": col_name, "السعر": col_price, "المقاس": col_size, "اللون": col_color}.items() if not v]
    if missing:
        await status_msg.edit_text(
            f"❌ الأعمدة دي مش موجودة في الشيت: {', '.join(missing)}\n"
            f"الأعمدة اللي عندك: {', '.join(first_keys)}\n\n"
            "الأعمدة المطلوبة:\n"
            "*اسم المنتج | السعر | المقاس | اللون | الكمية*",
            parse_mode="Markdown"
        )
        return

    for i, row in enumerate(raw_rows, start=2):
        name  = row.get(col_name, "").strip()
        price_raw = row.get(col_price, "0").strip().replace(",", "")
        size  = row.get(col_size, "").strip().upper()
        color = row.get(col_color, "").strip()
        qty_raw = row.get(col_qty, "1").strip() if col_qty else "1"

        if not name or not size or not color:
            errors.append(f"سطر {i}: بيانات ناقصة — تم تخطيه")
            continue

        try:
            price = float(price_raw or 0)
            qty   = int(float(qty_raw or 1))
        except ValueError:
            errors.append(f"سطر {i} ({name}): سعر أو كمية غلط")
            continue

        if price <= 0:
            errors.append(f"سطر {i} ({name}): السعر لازم أكبر من صفر")
            continue

        rows_parsed.append({
            "type":  "new_product",
            "name":  name,
            "price": price,
            "size":  size,
            "color": color,
            "qty":   max(1, qty),
        })

    if not rows_parsed:
        err_text = "\n".join(errors[:5])
        await status_msg.edit_text(f"❌ مفيش صفوف صالحة في الشيت!\n{err_text}")
        return

    # عرض ملخص للتأكيد
    pid = uuid.uuid4().hex[:12]
    store_pending_operation(context, pid, {
        "chat_id": chat_id,
        "tenant_id": tenant["id"],
        "items": rows_parsed,
    })

    preview_lines = [f"📊 *لقيت {len(rows_parsed)} صنف في الشيت:*\n"]
    for p in rows_parsed[:15]:  # بنعرض أول 15 بس
        preview_lines.append(f"• {p['name']} | {p['color']} {p['size']} | {p['price']:.0f}ج | كمية: {p['qty']}")
    if len(rows_parsed) > 15:
        preview_lines.append(f"... و{len(rows_parsed) - 15} صنف تاني")

    if errors:
        preview_lines.append(f"\n⚠️ *{len(errors)} سطر اتتخطى:*")
        for e in errors[:3]:
            preview_lines.append(f"  - {e}")

    preview_lines.append("\nتأكيد إضافة كل ده للمخزن؟")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأكيد الكل", callback_data=f"ok:{pid}"),
        InlineKeyboardButton("❌ إلغاء", callback_data=f"no:{pid}"),
    ]])

    await status_msg.edit_text("\n".join(preview_lines), reply_markup=keyboard, parse_mode="Markdown")


def main():
    # لازم على بايثون 3.14 علشان مايحصلش خطأ في الـ event loop
    asyncio.set_event_loop(asyncio.new_event_loop())

    async def post_init(application):
        async def background_checker():
            while True:
                await asyncio.sleep(1800)  # فحص كل 30 دقيقة
                try:
                    await check_subscriptions_and_notify(application)
                except Exception as err:
                    print("Error in subscription background checker:", err)

        asyncio.create_task(background_checker())
        asyncio.create_task(check_subscriptions_and_notify(application))

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("orders", orders_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("new_tenant", new_tenant_cmd))
    app.add_handler(CommandHandler("link_page", link_page_cmd))
    app.add_handler(CommandHandler("tenants", list_tenants_cmd))
    app.add_handler(CommandHandler("pause_bot", pause_bot_cmd))
    app.add_handler(CommandHandler("resume_bot", resume_bot_cmd))
    app.add_handler(CommandHandler("delete_tenant", delete_tenant_cmd))
    app.add_handler(CommandHandler("renew", renew_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    print("بوت التاجر شغال... (Ctrl+C للإيقاف)", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()