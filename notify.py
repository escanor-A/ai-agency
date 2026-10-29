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
    discount_line = ""
    if order.get("discount"):
        discount_line = f"🏷️ الخصم: {float(order['discount']):.0f} جنيه ({order.get('discount_note', '')})\n"
    elif order.get("discount_note"):
        discount_line = f"🏷️ عرض: {order['discount_note']}\n"

    text = (
        f"🛒 أوردر جديد #{order['order_number']}\n\n"
        f"👤 {customer_name}\n"
        f"📞 {phone}\n"
        f"📍 {governorate} - {address}\n\n"
        f"{items}\n"
        f"{discount_line}"
        f"🚚 الشحن: {float(order['shipping_fee']):.0f} جنيه\n"
        f"💰 الإجمالي: {float(order['total']):.0f} جنيه (الدفع عند الاستلام)"
    )
    return send_telegram(tenant_id, text)



def send_review_notification(tenant_id, customer_name, feedback, rating=5):
    """إرسال إشعار فوري للتاجر على تليجرام عند استلام ريفيو أو كلام حلو من عميل"""
    stars = "⭐" * max(1, min(5, int(rating)))
    text = (
        f"🌟 ريفيو وكلام حلو من عميل! {stars}\n\n"
        f"👤 العميل: {customer_name}\n"
        f"💬 \"{feedback}\"\n\n"
        f"📸 تقدر تاخدها سكرين شوت وتشاركها ستوري لزيادة مبيعاتك وثقة الزباين! 🚀"
    )
    return send_telegram(tenant_id, text)


def send_order_cancellation_notification(tenant_id, order_number, customer_name="", reason=""):
    """إرسال إشعار فوري للتاجر عند إلغاء أوردر ورجوع البضاعة للمخزن"""
    reason_line = f"\nسبب الإلغاء: {reason}" if reason else ""
    cust_line = f"👤 العميل: {customer_name}\n" if customer_name else ""
    text = (
        f"❌ تم إلغاء الأوردر #{order_number}\n\n"
        f"{cust_line}"
        f"🔄 تم إرجاع جميع كميات الأصناف للمخزن تلقائياً.{reason_line}"
    )
    return send_telegram(tenant_id, text)


def send_order_update_notification(tenant_id, order_number, changes_desc):
    """إرسال إشعار فوري للتاجر عند تعديل بيانات أوردر"""
    text = (
        f"✏️ تحديث في الأوردر #{order_number}\n\n"
        f"📋 التعديل الجديد:\n{changes_desc}"
    )
    return send_telegram(tenant_id, text)


def send_complaint_notification(tenant_id, customer_name, comment_text, post_text="", comment_id="", platform="facebook"):
    """إرسال إشعار فوري للتاجر على تليجرام عند رصد شكوى عميل في التعليقات بدون رد علني"""
    post_info = f"\n📌 على منشور: {post_text[:100]}..." if post_text else ""
    cid_info = f"\n🆔 معرّف التعليق: `{comment_id}`" if comment_id else ""
    platform_label = "فيسبوك" if platform == "facebook" else ("إنستجرام" if platform == "instagram" else "تيك توك")
    text = (
        f"🚨 *تنبيه شكوى عميل على {platform_label}!* 🚨\n\n"
        f"👤 العميل: {customer_name}\n"
        f"💬 نص التعليق: \"{comment_text}\""
        f"{post_info}"
        f"{cid_info}\n\n"
        f"⚠️ تم حجب الرد الآلي حمايةً لسمعة البراند، يرجى مراجعة العميل والتواصل معه فوراً لحل المشكلة! 📞"
    )
    return send_telegram(tenant_id, text)


def submit_praise_review(tenant_id: str, customer_name: str, feedback: str, rating: int = 5):
    """تسجيل ريفيو إيجابي في قاعدة البيانات وإشعار التاجر به"""
    try:
        supabase.table("reviews").insert({
            "tenant_id": tenant_id,
            "customer_name": customer_name,
            "rating": rating,
            "feedback": feedback,
        }).execute()
    except Exception as e:
        print(f"Error inserting review into DB: {e}")
    try:
        send_review_notification(tenant_id, customer_name, feedback, rating=rating)
    except Exception as e:
        print(f"Error sending review notification: {e}")
