import json
import os
from datetime import datetime, timedelta, timezone

SUBSCRIPTIONS_FILE = os.path.join(os.path.dirname(__file__), "subscriptions.json")


def load_subscriptions() -> dict:
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return {}
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_subscriptions(data: dict):
    with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_or_renew_subscription(tenant_id: str, months: int = 1) -> dict:
    """
    تجديد أو بدء اشتراك تاجر لعدد شهور معين (1 أو 3 أو غيرها).
    إذا كان الاشتراك سارياً يضيف المدة فوق التاريخ الحالي،
    وإذا كان منتهياً أو جديداً يبدأ من تاريخ اليوم.
    """
    subs = load_subscriptions()
    now = datetime.now(timezone.utc)
    
    current_end = None
    if tenant_id in subs and subs[tenant_id].get("ends_at"):
        try:
            current_end = datetime.fromisoformat(subs[tenant_id]["ends_at"])
        except Exception:
            current_end = None

    # لو لسه شغال نمد التاريخ، لو منتهي نبدأ من دلوقتي
    start_date = current_end if (current_end and current_end > now) else now
    
    # حساب تاريخ الانتهاء (الشهر التقريبي = 30 يوم)
    days_to_add = months * 30
    ends_at = start_date + timedelta(days=days_to_add)

    subs[tenant_id] = {
        "ends_at": ends_at.isoformat(),
        "months": months,
        "updated_at": now.isoformat(),
        "warning_sent": False,
        "expired_alert_sent": False,
    }
    save_subscriptions(subs)
    return {
        "ends_at": ends_at,
        "days_left": (ends_at - now).days,
        "is_active": True,
    }


def get_tenant_subscription(tenant_id: str) -> dict:
    subs = load_subscriptions()
    now = datetime.now(timezone.utc)
    
    if tenant_id not in subs or not subs[tenant_id].get("ends_at"):
        return {
            "has_subscription": False,
            "is_active": False,
            "days_left": 0,
            "ends_at": None,
            "ends_at_str": "غير محدد",
        }

    info = subs[tenant_id]
    try:
        ends_at = datetime.fromisoformat(info["ends_at"])
    except Exception:
        return {
            "has_subscription": False,
            "is_active": False,
            "days_left": 0,
            "ends_at": None,
            "ends_at_str": "غير صالح",
        }

    time_diff = ends_at - now
    days_left = time_diff.days
    hours_left = int(time_diff.total_seconds() // 3600)
    is_active = time_diff.total_seconds() > 0

    return {
        "has_subscription": True,
        "is_active": is_active,
        "days_left": days_left,
        "hours_left": hours_left,
        "ends_at": ends_at,
        "ends_at_str": ends_at.strftime("%Y-%m-%d"),
        "warning_sent": info.get("warning_sent", False),
        "expired_alert_sent": info.get("expired_alert_sent", False),
    }


def check_and_update_subscriptions():
    """
    فحص كل الاشتراكات:
    - إرجاع التجار اللي متبقي لهم <= 3 أيام لتنبيههم
    - إرجاع التجار اللي انتهى اشتراكهم لإيقاف البوت وتنبيههم
    """
    subs = load_subscriptions()
    now = datetime.now(timezone.utc)
    
    warnings_needed = []
    expirations_needed = []
    updated = False

    for tenant_id, info in subs.items():
        try:
            ends_at = datetime.fromisoformat(info["ends_at"])
        except Exception:
            continue

        time_left = ends_at - now
        days_left = time_left.days
        seconds_left = time_left.total_seconds()

        # 1. انتهى الاشتراك
        if seconds_left <= 0:
            if not info.get("expired_alert_sent", False):
                expirations_needed.append({
                    "tenant_id": tenant_id,
                    "ends_at": ends_at,
                })
                info["expired_alert_sent"] = True
                updated = True

        # 2. تحذير مسبق (3 أيام أو أقل)
        elif days_left <= 3:
            if not info.get("warning_sent", False):
                warnings_needed.append({
                    "tenant_id": tenant_id,
                    "days_left": max(days_left, 1),
                    "ends_at": ends_at,
                })
                info["warning_sent"] = True
                updated = True

    if updated:
        save_subscriptions(subs)

    return {
        "warnings": warnings_needed,
        "expirations": expirations_needed,
    }
