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
from check_stock import get_demo_tenant_id, supabase
from orders import cancel_order, update_order_shipping, update_order_status

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

# مهم: مكتبات الشبكة ممكن تطبع رابط فيه التوكن لو المستوى INFO، فبنقفلها
logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-3.5-flash-lite"
 # غيّره لو الاسم اتغير في Google AI Studio

# البوت ده بتاع تاجر واحد (بوت لكل تاجر). للتجربة: التاجر التجريبي.
BOT_TENANT_ID = get_demo_tenant_id()
SUPER_ADMIN_CHAT_ID = int(os.environ.get("SUPER_ADMIN_CHAT_ID", "1464362905"))

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat_id = update.effective_chat.id
    code = context.args[0].strip().upper() if context.args else None

    if code:
        # البحث عن الكود في كل التجار
        rows = (
            supabase.table("tenants")
            .select("id,name")
            .eq("telegram_link_code", code)
            .execute()
            .data
        )
        if not rows:
            await msg.reply_text("الكود غلط أو اتستخدم قبل كده.")
            return
        
        target_tenant = rows[0]
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
    if not get_owner_tenant(update.effective_chat.id):
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return
    await update.message.reply_text(
        "تقدر تكتبلي بالمصري، أمثلة:\n"
        "• بعت 2 تيشيرت أسود L\n"
        "• وصل 20 هودي رمادي L\n"
        "• المتاح من التيشيرت الأبيض؟\n\n"
        "أوامر:\n"
        "/orders - آخر 5 أوردرات\n"
        "/today - ملخص آخر 24 ساعة\n"
        "/cancel <رقم_الأوردر> - إلغاء أوردر ورجوع بضاعته للمخزن"
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not get_owner_tenant(update.effective_chat.id):
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return
    if not context.args:
        await update.message.reply_text("اكتب رقم الأوردر بعد الأمر، مثلاً:\n/cancel 1")
        return

    order_num = context.args[0].strip().replace("#", "")
    res = cancel_order(BOT_TENANT_ID, order_num)
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

    # استخراج الاسم والموبايل إن وجد
    import re
    full_text = " ".join(context.args)
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
    link_code = secrets.token_hex(3).upper()
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
        await msg.reply_text(
            f"🎉 *تم إنشاء حساب التاجر بنجاح!*\n\n"
            f"🏢 *المحل:* `{t['name']}`\n"
            f"🔑 *كود الربط:* `{link_code}`\n\n"
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

    # التحقق من وجود التاجر
    try:
        rows = (
            supabase.table("tenants")
            .select("id,name")
            .eq("id", tenant_id_str)
            .execute()
            .data
        )
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ في الاتصال بقاعدة البيانات: {e}")
        return

    if not rows:
        await update.message.reply_text(f"❌ مفيش تاجر بالـ ID: `{tenant_id_str}`\nاستخدم /tenants تشوف الأرقام الصح.", parse_mode="Markdown")
        return

    tenant = rows[0]
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
        supabase.table("tenants").update(updates).eq("id", tenant_id_str).execute()
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


async def list_tenants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للـ Super Admin يشوف كل التجار وأرقامهم."""
    chat_id = update.effective_chat.id
    if chat_id != SUPER_ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ الأمر ده للـ Super Admin بس.")
        return

    rows = (
        supabase.table("tenants")
        .select("id,name,facebook_page_id,instagram_account_id,whatsapp_phone_number_id,tiktok_account_id,telegram_chat_id")
        .order("id")
        .execute()
        .data
    )
    if not rows:
        await update.message.reply_text("مفيش تجار مسجلين لحد دلوقتي.")
        return

    lines = ["📋 *التجار المسجلين:*\n"]
    for t in rows:
        fb = f"📘 {t['facebook_page_id']}" if t.get("facebook_page_id") else "📘 ❌"
        ig = f"📸 {t['instagram_account_id']}" if t.get("instagram_account_id") else "📸 ❌"
        wa = f"🟢 {t['whatsapp_phone_number_id']}" if t.get("whatsapp_phone_number_id") else "🟢 ❌"
        tt = f"🎵 {t['tiktok_account_id']}" if t.get("tiktok_account_id") else "🎵 ❌"
        tg = f"✈️ مربوط" if t.get("telegram_chat_id") else "✈️ ❌"
        lines.append(
            f"*{t['id']}.* {t['name']}\n"
            f"   {fb} | {ig} | {wa} | {tt} | {tg}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not get_owner_tenant(update.effective_chat.id):
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return
    rows = (
        supabase.table("orders")
        .select("order_number,customer_name,governorate,total,status")
        .eq("tenant_id", BOT_TENANT_ID)
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
    if not get_owner_tenant(update.effective_chat.id):
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = (
        supabase.table("orders")
        .select("total,status")
        .eq("tenant_id", BOT_TENANT_ID)
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


def run_llm(chat_id: int, content, tools: list):
    """بيشغّل الموديل مع الأدوات وسياق المحادثة المحفوظ (يدعم النص والصوت)"""
    config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, tools=tools)
    
    # لو الجلسة مش موجودة بننشئها
    if chat_id not in MERCHANT_SESSIONS:
        MERCHANT_SESSIONS[chat_id] = client.chats.create(model=MODEL, config=config)
    else:
        MERCHANT_SESSIONS[chat_id]._config = config

    chat = MERCHANT_SESSIONS[chat_id]
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
    context.application.bot_data.setdefault("pending", {})[pid] = {
        "chat_id": chat_id,
        "tenant_id": tenant_id,
        "items": proposals,
    }
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
    context.application.bot_data.setdefault("pending", {})[pid] = {
        "chat_id": chat_id,
        "tenant_id": tenant_id,
        "items": proposals,
    }
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

    # pop = العملية تتنفذ مرة واحدة بس حتى لو الزرار اتداس مرتين
    pending = context.application.bot_data.get("pending", {}).pop(pid, None)
    if not pending or pending["chat_id"] != chat_id or not get_owner_tenant(chat_id):
        await query.edit_message_text("العملية دي انتهت أو اتنفذت قبل كده.")
        return

    tenant_id = pending.get("tenant_id", BOT_TENANT_ID)

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
    context.application.bot_data.setdefault("pending", {})[pid] = {
        "chat_id": chat_id,
        "tenant_id": tenant["id"],
        "items": rows_parsed,
    }

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

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("orders", orders_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("new_tenant", new_tenant_cmd))
    app.add_handler(CommandHandler("link_page", link_page_cmd))
    app.add_handler(CommandHandler("tenants", list_tenants_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    print("بوت التاجر شغال... (Ctrl+C للإيقاف)")
    app.run_polling()


if __name__ == "__main__":
    main()