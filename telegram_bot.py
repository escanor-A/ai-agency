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
from orders import cancel_order

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

# مهم: مكتبات الشبكة ممكن تطبع رابط فيه التوكن لو المستوى INFO، فبنقفلها
logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-3.5-flash"
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
7. لو صاحب المحل طلب إضافة منتج جديد (مش موجود قبل كده في المخزن): اطلب أو استخرج (اسم المنتج، السعر، المقاس، اللون، الكمية) ونادِ propose_new_product.

معلومات: المقاسات M و L و XL و 40 و 41 و 42 و 43 وغيرها. الألوان بالعربي: أسود، أبيض، رمادي، كحلي، بني، بيج.
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


def run_llm(text, tools):
    """بيشغّل الموديل مع الأدوات. بيعيد المحاولة مع 503 بس، مش مع 429"""
    config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, tools=tools)
    for attempt in range(3):
        try:
            chat = client.chats.create(model=MODEL, config=config)
            return chat.send_message(text).text or "ماقدرتش أفهم، ممكن توضّح؟"
        except Exception as e:
            msg = str(e)
            if "429" in msg:
                print("وصلنا لحد الحصة المجانية:", msg[:300])
                return "وصلنا لحد الاستخدام المجاني للذكاء الاصطناعي دلوقتي، جرّب بعد شوية."
            if "503" in msg and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            print("خطأ من الموديل:", type(e).__name__, msg[:300])
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

    reply = await asyncio.to_thread(
        run_llm, update.message.text, [check_stock, propose_stock_change, propose_new_product]
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
    for i, item in enumerate(pending["items"]):
        if item.get("type") == "new_product":
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
    elif success_new_products:
        await query.edit_message_text(f"تم إضافة المنتج بنجاح ✅:\n" + "\n".join(f"• {p}" for p in success_new_products))
    else:
        await query.edit_message_text("تم ✅ المخزون اتحدّث.")


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
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("بوت التاجر شغال... (Ctrl+C للإيقاف)")
    app.run_polling()


if __name__ == "__main__":
    main()