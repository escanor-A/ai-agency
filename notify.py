import os

import httpx

from check_stock import supabase


def send_telegram(tenant_id, text):
    """
    بيبعت رسالة لتاجر معيّن على تليجرام.
    دلوقتي التوكن من ملف .env (للتجربة بتاعة بوت واحد).
    لاحقاً: كل تاجر ليه توكن مخزّن بأمان.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False

    rows = (
        supabase.table("tenants")
        .select("telegram_chat_id")
        .eq("id", tenant_id)
        .execute()
        .data
    )
    if not rows or not rows[0]["telegram_chat_id"]:
        return False  # التاجر لسه ما ربطش تليجرام

    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": rows[0]["telegram_chat_id"], "text": text},
            timeout=5,
        )
        return response.status_code == 200
    except Exception as e:
        # بنطبع نوع الخطأ بس، لأن رسالة الخطأ ممكن تحتوي على التوكن
        print("فشل إرسال إشعار تليجرام:", type(e).__name__)
        return False


def send_order_notification(tenant_id, order, customer_name, phone, governorate, address, lines):
    items = "\n".join(
        f"• {l['qty']} × {l['name']} ({l['color']} - {l['size']}): {l['line_total']:.0f} جنيه"
        for l in lines
    )
    text = (
        f"🛒 أوردر جديد #{order['order_number']}\n\n"
        f"👤 {customer_name}\n"
        f"📞 {phone}\n"
        f"📍 {governorate} - {address}\n\n"
        f"{items}\n"
        f"🚚 الشحن: {float(order['shipping_fee']):.0f} جنيه\n"
        f"💰 الإجمالي: {float(order['total']):.0f} جنيه (الدفع عند الاستلام)"
    )
    return send_telegram(tenant_id, text)