import json
import os
import sys
from datetime import datetime, timedelta, timezone
from check_stock import supabase

sys.stdout.reconfigure(encoding="utf-8")

SUBSCRIPTIONS_FILE = os.path.join(os.path.dirname(__file__), "subscriptions.json")

# فحص إذا كان جدول subscriptions متاحاً في قاعدة البيانات
_DB_TABLE_AVAILABLE = None


def is_db_table_available() -> bool:
    """فحص الاتصال بجدول subscriptions في قاعدة البيانات Supabase"""
    global _DB_TABLE_AVAILABLE
    if _DB_TABLE_AVAILABLE is not None:
        return _DB_TABLE_AVAILABLE
    try:
        supabase.table("subscriptions").select("id").limit(1).execute()
        _DB_TABLE_AVAILABLE = True
        # محاولة ترحيل البيانات القديمة من JSON إلى الداتابيز تلقائياً
        migrate_local_json_to_db()
    except Exception as e:
        _DB_TABLE_AVAILABLE = False
        print("⚠️ جدول subscriptions غير موجود في Supabase حالياً، سيتم استخدام ملف subscriptions.json كاحتياطي مؤقت.")
        print("💡 لتفعيل الداتابيز، نفذ ملف docs/subscriptions_table.sql في Supabase SQL Editor.")
    return _DB_TABLE_AVAILABLE


def migrate_local_json_to_db():
    """ترحيل الاشتراكات القديمة من subscriptions.json إلى قاعدة البيانات تلقائياً لمنع ضياعها"""
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            local_data = json.load(f)
        if not local_data:
            return

        migrated_count = 0
        for tid, info in local_data.items():
            if not info.get("ends_at"):
                continue
            # فحص إذا كان موجوداً مسبقاً بالداتابيز
            existing = supabase.table("subscriptions").select("id").eq("tenant_id", tid).limit(1).execute().data
            if not existing:
                row = {
                    "tenant_id": tid,
                    "ends_at": info["ends_at"],
                    "months": info.get("months", 1),
                    "warning_sent": info.get("warning_sent", False),
                    "expired_alert_sent": info.get("expired_alert_sent", False),
                    "updated_at": info.get("updated_at", datetime.now(timezone.utc).isoformat()),
                }
                supabase.table("subscriptions").insert(row).execute()
                migrated_count += 1
        if migrated_count > 0:
            print(f"📦 تم ترحيل {migrated_count} اشتراك بنجاح من subscriptions.json إلى قاعدة البيانات Supabase!")
    except Exception as e:
        print("تنبيه ترحيل الاشتراكات:", e)


# -------------------------------------------------------------------
# دوال الاحتياط المحلي (Fallback) في حالة عدم تفعيل الجدول بعد
# -------------------------------------------------------------------
def _load_subscriptions_json() -> dict:
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return {}
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_subscriptions_json(data: dict):
    try:
        with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# -------------------------------------------------------------------
# العمليات الأساسية للاشتراكات (تفضيل الداتابيز مع السقوط الآمن على JSON)
# -------------------------------------------------------------------
def add_or_renew_subscription(tenant_id: str, months: int = 1) -> dict:
    """
    تجديد أو بدء اشتراك تاجر لعدد شهور معين وحفظه في جدول subscriptions بقاعدة البيانات.
    إذا كان الاشتراك سارياً يضيف المدة فوق التاريخ الحالي،
    وإذا كان منتهياً أو جديداً يبدأ من تاريخ اليوم.
    """
    now = datetime.now(timezone.utc)
    current_end = None

    if is_db_table_available():
        try:
            rows = supabase.table("subscriptions").select("ends_at").eq("tenant_id", tenant_id).limit(1).execute().data
            if rows and rows[0].get("ends_at"):
                current_end = datetime.fromisoformat(rows[0]["ends_at"].replace("Z", "+00:00"))
        except Exception as e:
            print("خطأ في جلب الاشتراك من الداتابيز:", e)
    else:
        subs = _load_subscriptions_json()
        if tenant_id in subs and subs[tenant_id].get("ends_at"):
            try:
                current_end = datetime.fromisoformat(subs[tenant_id]["ends_at"])
            except Exception:
                current_end = None

    # لو لسه شغال نمد التاريخ، لو منتهي نبدأ من دلوقتي
    start_date = current_end if (current_end and current_end > now) else now
    days_to_add = months * 30
    ends_at = start_date + timedelta(days=days_to_add)

    sub_payload = {
        "tenant_id": tenant_id,
        "ends_at": ends_at.isoformat(),
        "months": months,
        "warning_sent": False,
        "expired_alert_sent": False,
        "updated_at": now.isoformat(),
    }

    # الحفظ في قاعدة البيانات
    if is_db_table_available():
        try:
            supabase.table("subscriptions").upsert(sub_payload, on_conflict="tenant_id").execute()
            # تفعيل البوت فوراً في جدول tenants بعد الدفع
            supabase.table("tenants").update({"bot_enabled": True}).eq("id", tenant_id).execute()
        except Exception as e:
            print("خطأ في حفظ الاشتراك بالداتابيز:", e)
            _fallback_save(tenant_id, sub_payload)
    else:
        _fallback_save(tenant_id, sub_payload)

    return {
        "ends_at": ends_at,
        "days_left": (ends_at - now).days,
        "is_active": True,
    }


def _fallback_save(tenant_id: str, sub_payload: dict):
    subs = _load_subscriptions_json()
    subs[tenant_id] = sub_payload
    _save_subscriptions_json(subs)


def get_tenant_subscription(tenant_id: str) -> dict:
    """جلب حالة اشتراك التاجر من قاعدة البيانات مباشرة لضمان عدم ضياعها مع كل نشر"""
    now = datetime.now(timezone.utc)
    info = None

    if is_db_table_available():
        try:
            rows = supabase.table("subscriptions").select("*").eq("tenant_id", tenant_id).limit(1).execute().data
            if rows:
                info = rows[0]
        except Exception as e:
            print("خطأ في جلب الاشتراك من الداتابيز:", e)

    if not info:
        subs = _load_subscriptions_json()
        info = subs.get(tenant_id)

    if not info or not info.get("ends_at"):
        return {
            "has_subscription": False,
            "is_active": False,
            "days_left": 0,
            "ends_at": None,
            "ends_at_str": "غير محدد",
        }

    try:
        raw_ends_at = info["ends_at"].replace("Z", "+00:00")
        ends_at = datetime.fromisoformat(raw_ends_at)
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


def check_and_update_subscriptions() -> dict:
    """
    فحص كل الاشتراكات في قاعدة البيانات:
    - إرجاع التجار المتبقي لهم <= 3 أيام لتنبيههم
    - إرجاع التجار المنتهي اشتراكهم وإيقاف البوت تلقائياً في قاعدة البيانات
    """
    now = datetime.now(timezone.utc)
    warnings_needed = []
    expirations_needed = []

    if is_db_table_available():
        try:
            rows = supabase.table("subscriptions").select("*").execute().data or []
            for sub in rows:
                if not sub.get("ends_at"):
                    continue
                try:
                    ends_at = datetime.fromisoformat(sub["ends_at"].replace("Z", "+00:00"))
                except Exception:
                    continue

                time_left = ends_at - now
                days_left = time_left.days
                seconds_left = time_left.total_seconds()
                tid = sub["tenant_id"]

                # 1. اشتراك منتهي
                if seconds_left <= 0:
                    if not sub.get("expired_alert_sent", False):
                        expirations_needed.append({"tenant_id": tid, "ends_at": ends_at})
                        # إيقاف البوت في جدول tenants وتحديث حالة التنبيه
                        supabase.table("tenants").update({"bot_enabled": False}).eq("id", tid).execute()
                        supabase.table("subscriptions").update({"expired_alert_sent": True}).eq("id", sub["id"]).execute()

                # 2. تحذير مسبق (3 أيام أو أقل)
                elif days_left <= 3:
                    if not sub.get("warning_sent", False):
                        warnings_needed.append({"tenant_id": tid, "days_left": max(days_left, 1), "ends_at": ends_at})
                        supabase.table("subscriptions").update({"warning_sent": True}).eq("id", sub["id"]).execute()

            return {"warnings": warnings_needed, "expirations": expirations_needed}
        except Exception as e:
            print("خطأ في فحص الاشتراكات بالداتابيز:", e)

    # Fallback to JSON
    subs = _load_subscriptions_json()
    updated = False
    for tenant_id, info in subs.items():
        try:
            ends_at = datetime.fromisoformat(info["ends_at"].replace("Z", "+00:00"))
        except Exception:
            continue

        time_left = ends_at - now
        days_left = time_left.days
        seconds_left = time_left.total_seconds()

        if seconds_left <= 0:
            if not info.get("expired_alert_sent", False):
                expirations_needed.append({"tenant_id": tenant_id, "ends_at": ends_at})
                info["expired_alert_sent"] = True
                updated = True
        elif days_left <= 3:
            if not info.get("warning_sent", False):
                warnings_needed.append({"tenant_id": tenant_id, "days_left": max(days_left, 1), "ends_at": ends_at})
                info["warning_sent"] = True
                updated = True

    if updated:
        _save_subscriptions_json(subs)

    return {"warnings": warnings_needed, "expirations": expirations_needed}
