import argparse
import secrets
import sys
import time

from check_stock import supabase
from subscriptions import add_or_renew_subscription
from merchant_settings import set_merchant_channels, normalize_channel_name, SUPPORTED_CHANNELS

sys.stdout.reconfigure(encoding="utf-8")


def add_tenant(name: str, telegram_bot_token: str = None, owner_phone: str = None, months: int = 1, channels: str = None):
    """إضافة تاجر جديد في الداتابيز وتوليد كود ربط قوي لتليجرام (16 حرف وصلاحية 24 ساعة) وتفعيل اشتراكه وقنواته"""
    raw_code = secrets.token_hex(8).upper()
    expiry_ts = int(time.time() + 24 * 3600)
    link_code = f"{raw_code}_{expiry_ts}"

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

    # ضبط القنوات المشترك فيها التاجر
    active_chs = []
    if channels:
        ch_payload = {ch: False for ch in SUPPORTED_CHANNELS}
        for tok in channels.replace(",", " ").split():
            norm = normalize_channel_name(tok)
            if norm in SUPPORTED_CHANNELS:
                ch_payload[norm] = True
                active_chs.append(norm)
        set_merchant_channels(tenant["id"], ch_payload)
    else:
        # الافتراضي: فيسبوك فقط
        set_merchant_channels(tenant["id"], {"facebook": True, "instagram": False, "tiktok": False, "whatsapp": False})
        active_chs = ["facebook"]

    # تفعيل الاشتراك تلقائياً زي أمر /new_tenant
    sub = add_or_renew_subscription(tenant["id"], months=months)
    ends_str = sub["ends_at"].strftime("%Y-%m-%d") if hasattr(sub.get("ends_at"), "strftime") else str(sub.get("ends_at"))

    print("\n" + "=" * 50)
    print("✅ تم إنشاء حساب التاجر بنجاح في قاعدة البيانات!")
    print(f"🏢 اسم المحل: {tenant['name']}")
    print(f"🆔 كود التاجر (Tenant ID): {tenant['id']}")
    print(f"🔑 كود الربط على تليجرام: {tenant['telegram_link_code']}")
    print(f"📅 مدة الاشتراك: {months} شهر (ساري حتى: {ends_str})")
    print(f"📱 القنوات المفعلة: {', '.join(active_chs)}")
    if tenant.get("telegram_bot_token"):
        print("🤖 تم تسجيل توكن البوت بنجاح")
    print("=" * 50)
    print(f"\nلربط البوت بصاحب المحل، خليه يدخل على البوت ويكتب:")
    print(f"👉 /start {tenant['telegram_link_code']}\n")
    return tenant


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="إضافة تاجر جديد للسيستم")
    parser.add_argument("--name", required=True, help="اسم المحل أو البراند")
    parser.add_argument("--token", required=False, help="توكن بوت التليجرام الخاص بالتاجر (من BotFather)")
    parser.add_argument("--phone", required=False, help="رقم موبايل صاحب المحل")
    parser.add_argument("--months", type=int, default=1, help="مدة الاشتراك بالشهور (الافتراضي 1)")
    parser.add_argument("--channels", required=False, help="قنوات السوشيال ميديا المفعلة (مثال: fb,ig,tiktok,wa)")

    args = parser.parse_args()
    add_tenant(args.name, args.token, args.phone, months=args.months, channels=args.channels)
