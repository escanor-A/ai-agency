import argparse
import secrets
import sys

from check_stock import supabase

sys.stdout.reconfigure(encoding="utf-8")


def add_tenant(name: str, telegram_bot_token: str = None, owner_phone: str = None):
    """إضافة تاجر جديد في الداتابيز وتوليد كود ربط لتليجرام"""
    # توليد كود ربط عشوائي سهل القراءة (6 حروف وأرقام)
    link_code = secrets.token_hex(3).upper()

    data = {
        "name": name.strip(),
        "bot_enabled": True,
        "telegram_link_code": link_code,
    }
    if telegram_bot_token:
        data["telegram_bot_token"] = telegram_bot_token.strip()
    if owner_phone:
        data["owner_phone"] = owner_phone.strip()

    res = supabase.table("tenants").insert(data).execute()
    if not res.data:
        print("❌ فشل إضافة التاجر")
        return None

    tenant = res.data[0]
    print("\n" + "=" * 50)
    print("✅ تم إنشاء حساب التاجر بنجاح في قاعدة البيانات!")
    print(f"🏢 اسم المحل: {tenant['name']}")
    print(f"🆔 كود التاجر (Tenant ID): {tenant['id']}")
    print(f"🔑 كود الربط على تليجرام: {tenant['telegram_link_code']}")
    if tenant.get("telegram_bot_token"):
        print(f"🤖 توكن البوت: {tenant['telegram_bot_token'][:15]}...")
    print("=" * 50)
    print(f"\nلربط البوت بصاحب المحل، خليه يدخل على البوت ويكتب:")
    print(f"👉 /start {tenant['telegram_link_code']}\n")
    return tenant


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="إضافة تاجر جديد للسيستم")
    parser.add_argument("--name", required=True, help="اسم المحل أو البراند")
    parser.add_argument("--token", required=False, help="توكن بوت التليجرام الخاص بالتاجر (من BotFather)")
    parser.add_argument("--phone", required=False, help="رقم موبايل صاحب المحل")

    args = parser.parse_args()
    add_tenant(args.name, args.token, args.phone)
