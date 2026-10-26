import os
import sys

from dotenv import load_dotenv
from supabase import create_client

from check_stock import supabase
from customer_agent import handle_customer_message
from subscriptions import add_or_renew_subscription, get_tenant_subscription

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()


def main():
    print("=" * 60)
    print("🧪 محاكي اختبار وكالة الـ AI — Omnichannel Agency Tester")
    print("=" * 60)

    # جلب التجار من قاعدة البيانات
    rows = (
        supabase.table("tenants")
        .select("id, name, bot_enabled, telegram_chat_id")
        .order("name")
        .execute()
        .data or []
    )

    if not rows:
        print("❌ لا يوجد تجار مسجلين حالياً. أنشئ تاجر أولاً عبر تليجرام بـ /new_tenant.")
        return

    print("\n📋 قائمة التجار المتاحين للاختبار:")
    for i, t in enumerate(rows, 1):
        status = "🟢 مفعل" if t.get("bot_enabled", True) else "🔴 موقوف"
        sub = get_tenant_subscription(t["id"])
        sub_info = f"{sub['days_left']} يوم باقي" if sub["has_subscription"] else "غير محدد"
        print(f"[{i}] {t['name']} | الحالة: {status} | اشتراك: {sub_info}")

    choice = input("\n👉 اختر رقم التاجر الذي تريد تجربة محادثة زبون معه (مثلاً 1): ").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(rows)):
        print("اختيار غير صالح.")
        return

    tenant = rows[int(choice) - 1]
    tid = tenant["id"]
    tname = tenant["name"]

    print("\n" + "=" * 60)
    print(f"🏪 أنت الآن تتحدث كعميل لمتجر: «{tname}»")
    print("💡 جرب تسأل عن بضاعة، أسعار، مقاسات، أو اطلب أوردر كامل.")
    print("💡 اكتب 'خروج' أو 'exit' للرجوع.")
    print("=" * 60 + "\n")

    # فحص إذا كان البوت موقوفاً
    if not tenant.get("bot_enabled", True):
        print("⚠️ تنبيه: هذا التاجر حسابه موقوف حالياً (لم يدفع الاشتراك).")
        print("في الوضع الفعلي، السيرفر سيتجاهل رسائل العملاء ولن يرد.")
        sub_test = input("هل تريد تفعيل اشتراكه شهراً الآن للاختبار؟ (نعم/لا): ").strip()
        if sub_test.lower() in ["نعم", "y", "yes"]:
            add_or_renew_subscription(tid, months=1)
            supabase.table("tenants").update({"bot_enabled": True}).eq("id", tid).execute()
            print("✅ تم تفعيل الاشتراك وتشغيل البوت! يمكنك التحدث الآن.\n")
        else:
            return

    customer_id = "test_customer_01"

    while True:
        try:
            user_msg = input("👤 أنت (العميل): ").strip()
            if not user_msg:
                continue
            if user_msg.lower() in ["خروج", "exit", "quit"]:
                print("تم إنهاء الاختبار 👋")
                break

            print("⏳ جاري توليد الرد من الذكاء الاصطناعي...")
            reply = handle_customer_message(tid, customer_id, user_msg)
            print(f"\n🤖 بوت «{tname}»:\n{reply}\n")
            print("-" * 60)
        except (KeyboardInterrupt, EOFError):
            print("\nتم إنهاء الاختبار.")
            break


if __name__ == "__main__":
    main()
