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

SYSTEM_PROMPT = """أنت مساعد مخزن لصاحب محل ملابس مصري، بتتكلم معاه على تليجرام.
أسلوبك: لهجة مصرية بسيطة، ردود قصيرة جداً.

قواعد:
1. لأي استعلام عن المخزون استخدم check_stock. ممنوع تخمّن كميات.
2. لو صاحب المحل قال إنه باع، أو وصلته بضاعة، أو في مرتجع: حدد الصنف بـ check_stock عشان تجيب كود الصنف (sku)، وبعدين نادِ propose_stock_change.
3. propose_stock_change مابتنفذش حاجة، هي بس بتجهّز العملية. الأزرار هي اللي هتخلي صاحب المحل يأكد. ماتقولش إن العملية اتنفذت أبداً.
4. لو المقاس أو اللون ناقص أو في أكتر من احتمال، اسأل بدل ما تخمّن.
5. لو الكمية ماتقالتش افترض 1.
6. أنواع العمليات: sale = بيع من المحل (بينقص)، restock = وصلت بضاعة (بتزيد)، return = مرتجع (بيزيد).

معلومات: المقاسات M و L و XL (لارج = L، ميديم = M). الألوان بالعربي: أسود، أبيض، رمادي، كحلي.
"""

NOT_OWNER_MESSAGE = "البوت ده خاص بصاحب المحل. لو إنت صاحب المحل، افتح رابط الربط اللي وصلك."


def get_owner_tenant(chat_id):
    """بيرجّع التاجر لو الشات ده هو شات المالك المربوط بالبوت، غير كده None"""
    rows = (
        supabase.table("tenants")
        .select("id,name")
        .eq("id", BOT_TENANT_ID)
        .eq("telegram_chat_id", chat_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    code = context.args[0].strip() if context.args else None

    if code:
        rows = (
            supabase.table("tenants")
            .select("id,name")
            .eq("id", BOT_TENANT_ID)
            .eq("telegram_link_code", code)
            .execute()
            .data
        )
        if not rows:
            await update.message.reply_text("الكود غلط أو اتستخدم قبل كده.")
            return
        supabase.table("tenants").update(
            {"telegram_chat_id": chat_id, "telegram_link_code": None}
        ).eq("id", BOT_TENANT_ID).execute()
        await update.message.reply_text(
            f"تم ربط البوت بـ «{rows[0]['name']}» ✅\n"
            "هيوصلك هنا إشعار بكل أوردر جديد.\n"
            "اكتب /help عشان تشوف تقدر تعمل إيه."
        )
        return

    tenant = get_owner_tenant(chat_id)
    if tenant:
        await update.message.reply_text(f"أهلاً بيك في بوت «{tenant['name']}» 👋\nاكتب /help.")
    else:
        await update.message.reply_text(NOT_OWNER_MESSAGE)


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
        "/today - ملخص آخر 24 ساعة"
    )


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
    if not get_owner_tenant(chat_id):
        await update.message.reply_text(NOT_OWNER_MESSAGE)
        return

    proposals = []  # العمليات اللي الموديل جهّزها وتستنى التأكيد

    def check_stock(product_name: str, size: str = "", color: str = "") -> dict:
        """بتكشف المخزون لمنتج بمقاس ولون معين، وبترجّع كود الصنف (sku) والكمية المتاحة.

        Args:
            product_name: اسم المنتج أو جزء منه (تيشيرت، هودي، بنطلون)
            size: المقاس M أو L أو XL (فاضي لو ماتحددش)
            color: اللون بالعربي (فاضي لو ماتحددش)
        """
        return db_check_stock(
            BOT_TENANT_ID, product_name, size=size or None, color=color or None
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
            .eq("tenant_id", BOT_TENANT_ID)
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
            {"variant_id": v["id"], "label": label, "change": change, "kind": kind}
        )
        return {"status": "waiting_confirmation", "item": label, "change": change}

    reply = await asyncio.to_thread(
        run_llm, update.message.text, [check_stock, propose_stock_change]
    )

    if not proposals:
        await update.message.reply_text(reply)
        return

    pid = uuid.uuid4().hex[:12]
    context.application.bot_data.setdefault("pending", {})[pid] = {
        "chat_id": chat_id,
        "items": proposals,
    }
    kinds = {"sale": "خصم (بيع من المحل)", "restock": "إضافة بضاعة", "return": "مرتجع"}
    summary = "\n".join(
        f"• {kinds[p['kind']]}: {abs(p['change'])} × {p['label']}" for p in proposals
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأكيد", callback_data=f"ok:{pid}"),
                InlineKeyboardButton("❌ إلغاء", callback_data=f"no:{pid}"),
            ]
        ]
    )
    await update.message.reply_text(
        f"هنفّذ التعديل ده:\n{summary}\n\nتأكيد؟", reply_markup=keyboard
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

    if action == "no":
        await query.edit_message_text("اتلغت ❌")
        return

    failures = []
    for i, item in enumerate(pending["items"]):
        try:
            supabase.rpc(
                "apply_stock_movement",
                {
                    "p_tenant": BOT_TENANT_ID,
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
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("بوت التاجر شغال... (Ctrl+C للإيقاف)")
    app.run_polling()


if __name__ == "__main__":
    main()