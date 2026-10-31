import json
import os
import threading
import time

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "merchant_settings.json")
_LOCK = threading.Lock()

# القنوات المدعومة والأسماء البديلة
SUPPORTED_CHANNELS = ("facebook", "instagram", "tiktok", "whatsapp")

CHANNEL_ALIASES = {
    "fb": "facebook",
    "facebook": "facebook",
    "messenger": "facebook",
    "فيس": "facebook",
    "فيسبوك": "facebook",
    "ماسينجر": "facebook",
    "ماسنجر": "facebook",
    "ig": "instagram",
    "insta": "instagram",
    "instagram": "instagram",
    "انستا": "instagram",
    "انستجرام": "instagram",
    "إنستجرام": "instagram",
    "إنستا": "instagram",
    "tt": "tiktok",
    "tik": "tiktok",
    "tiktok": "tiktok",
    "تيك": "tiktok",
    "تيكتوك": "tiktok",
    "تيك_توك": "tiktok",
    "تيك توك": "tiktok",
    "wa": "whatsapp",
    "whats": "whatsapp",
    "whatsapp": "whatsapp",
    "واتس": "whatsapp",
    "واتساب": "whatsapp",
    "الواتس": "whatsapp",
    "الواتساب": "whatsapp",
}

_CHANNELS_CACHE = {}  # {tenant_id: (timestamp, channels_dict)}
CHANNELS_CACHE_TTL = 30  # ثانية


def normalize_channel_name(channel: str) -> str:
    """توحيد اسم القناة ليطابق إحدى القنوات الأساسية المدعومة"""
    if not channel:
        return ""
    clean = str(channel).strip().lower()
    return CHANNEL_ALIASES.get(clean, clean)


def _read_all() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_all(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving merchant settings to JSON: {e}")


def load_merchant_settings(tenant_id: str) -> dict:
    """تحميل إعدادات التاجر الإضافية المخزنة محلياً"""
    if not tenant_id:
        return {}
    with _LOCK:
        data = _read_all()
        return data.get(str(tenant_id), {})


def save_merchant_settings(tenant_id: str, updates: dict) -> dict:
    """حفظ إعدادات التاجر الإضافية في Supabase ومحلياً كنسخة احتياطية"""
    if not tenant_id or not updates:
        return {}

    tid = str(tenant_id)
    # 1. المزامنة المباشرة مع قاعدة البيانات (Supabase)
    try:
        from check_stock import supabase
        db_updates = {}
        if "google_sheets_url" in updates:
            db_updates["google_sheets_url"] = updates["google_sheets_url"].strip() if updates["google_sheets_url"] else None
        if "shipping_enabled" in updates:
            db_updates["shipping_enabled"] = bool(updates["shipping_enabled"])

        # قنوات السوشيال ميديا
        for ch_key in ("allow_facebook", "allow_instagram", "allow_tiktok", "allow_whatsapp"):
            if ch_key in updates:
                db_updates[ch_key] = bool(updates[ch_key])

        if "whatsapp_monthly_limit" in updates:
            db_updates["whatsapp_monthly_limit"] = int(updates["whatsapp_monthly_limit"])
        if "whatsapp_used_conversations" in updates:
            db_updates["whatsapp_used_conversations"] = int(updates["whatsapp_used_conversations"])

        if db_updates:
            try:
                supabase.table("tenants").update(db_updates).eq("id", tid).execute()
            except Exception as dbe:
                # في حالة عدم تنفيذ ملف SQL بعد وعدم وجود الأعمدة
                print(f"Notice: saving channels to Supabase tenants failed (running local fallback): {dbe}")
    except Exception as e:
        print(f"Notice: saving to Supabase tenants failed: {e}")

    # 2. الحفظ المحلي كـ Cache واحتياطي
    with _LOCK:
        data = _read_all()
        if tid not in data:
            data[tid] = {}
        data[tid].update(updates)
        _write_all(data)

        # إفراغ الكاش اللحظي
        _CHANNELS_CACHE.pop(tid, None)
        return data[tid]


def get_merchant_google_sheets_url(tenant_id: str) -> str:
    """الحصول على رابط شيت المخزن للتاجر من الداتابيز مباشرة (مع الفولباك المحلي)"""
    if not tenant_id:
        return ""
    try:
        from check_stock import supabase
        res = supabase.table("tenants").select("google_sheets_url").eq("id", tenant_id).execute()
        if res.data and res.data[0].get("google_sheets_url"):
            return res.data[0]["google_sheets_url"]
    except Exception:
        pass
    return load_merchant_settings(tenant_id).get("google_sheets_url", "")


# ===================================================================
# إدارة قنوات السوشيال ميديا واشتراكاتها (Facebook, Instagram, TikTok, WhatsApp)
# ===================================================================

def get_merchant_channels(tenant_id: str, tenant_data: dict = None, fresh: bool = False) -> dict:
    """
    جلب القنوات المفعلة للتاجر حسب اشتراكه:
    - facebook: افتراضياً True
    - instagram: افتراضياً False (أو True لو مربوط بالفعل ومش معطل يدوياً)
    - tiktok: افتراضياً False (أو True لو مربوط بالفعل ومش معطل يدوياً)
    - whatsapp: افتراضياً False (لأن محادثات Meta Cloud API مدفوعة)
    """
    if not tenant_id:
        return {
            "facebook": False,
            "instagram": False,
            "tiktok": False,
            "whatsapp": False,
            "whatsapp_monthly_limit": 500,
            "whatsapp_used_conversations": 0,
        }

    tid = str(tenant_id)
    now = time.time()
    if not fresh and tid in _CHANNELS_CACHE:
        ts, cached_val = _CHANNELS_CACHE[tid]
        if now - ts < CHANNELS_CACHE_TTL:
            return cached_val

    # 1. فحص بيانات tenant_data إذا تم تمريرها من الويب هوك مسبقاً
    row = tenant_data if (tenant_data and isinstance(tenant_data, dict)) else {}

    # 2. إذا لم تكن البيانات كافية، استعلام قاعدة البيانات
    if not row or "allow_facebook" not in row:
        try:
            from check_stock import supabase
            res = (
                supabase.table("tenants")
                .select("allow_facebook, allow_instagram, allow_tiktok, allow_whatsapp, whatsapp_monthly_limit, whatsapp_used_conversations, facebook_page_id, instagram_account_id, tiktok_account_id, whatsapp_phone_number_id")
                .eq("id", tid)
                .limit(1)
                .execute()
            )
            if res.data:
                row = res.data[0]
        except Exception:
            # الأعمدة قد لا تكون موجودة بعد في قاعدة البيانات
            pass

    # 3. دمج مع الإعدادات المخزنة محلياً (Fallback)
    local_settings = load_merchant_settings(tid)

    # استخراج القيم مع الأخذ في الاعتبار الأولويات والقيم الافتراضية الذكية
    def _resolve_bool(key: str, default: bool) -> bool:
        if row and row.get(key) is not None:
            return bool(row[key])
        if key in local_settings:
            return bool(local_settings[key])
        return default

    # فيسبوك: افتراضياً True
    allow_fb = _resolve_bool("allow_facebook", True)

    # إنستجرام: لو متحدد explicitly ناخده، وإلا لو مربوط ومش محظور True
    ig_default = bool(row.get("instagram_account_id")) if row else False
    allow_ig = _resolve_bool("allow_instagram", ig_default)

    # تيك توك: لو متحدد explicitly ناخده، وإلا لو مربوط ومش محظور True
    tt_default = bool(row.get("tiktok_account_id")) if row else False
    allow_tt = _resolve_bool("allow_tiktok", tt_default)

    # واتساب: افتراضياً False إلا لو اتفعل صراحة بسبب تكلفة رسائل Meta
    allow_wa = _resolve_bool("allow_whatsapp", False)

    # حدود واستهلاك الواتساب
    wa_limit = 500
    if row and row.get("whatsapp_monthly_limit") is not None:
        wa_limit = int(row["whatsapp_monthly_limit"])
    elif "whatsapp_monthly_limit" in local_settings:
        wa_limit = int(local_settings["whatsapp_monthly_limit"])

    wa_used = 0
    if row and row.get("whatsapp_used_conversations") is not None:
        wa_used = int(row["whatsapp_used_conversations"])
    elif "whatsapp_used_conversations" in local_settings:
        wa_used = int(local_settings["whatsapp_used_conversations"])

    result = {
        "facebook": allow_fb,
        "instagram": allow_ig,
        "tiktok": allow_tt,
        "whatsapp": allow_wa,
        "whatsapp_monthly_limit": wa_limit,
        "whatsapp_used_conversations": wa_used,
    }

    _CHANNELS_CACHE[tid] = (now, result)
    return result


def is_channel_allowed(tenant_id: str, channel: str, tenant_data: dict = None) -> bool:
    """
    فحص فوري سريع: هل القناة مصرح بها ومفعلة باشتراك هذا التاجر؟
    channel يمكن أن تكون: facebook, messenger, instagram, ig, tiktok, tt, whatsapp, wa
    """
    norm = normalize_channel_name(channel)
    if not norm or norm not in SUPPORTED_CHANNELS:
        return True  # لو قناة غير معروفة لا نمنعها تلقائياً

    channels = get_merchant_channels(tenant_id, tenant_data=tenant_data)
    return bool(channels.get(norm, False))


def set_merchant_channels(tenant_id: str, updates: dict) -> dict:
    """
    تحديث القنوات المفعلة للتاجر (من خلال الأدمن أو بوت تليجرام):
    مثال: set_merchant_channels(tid, {"facebook": True, "instagram": True, "whatsapp": False, "tiktok": True})
    """
    if not tenant_id or not updates:
        return {}

    save_payload = {}
    for ch in SUPPORTED_CHANNELS:
        key = f"allow_{ch}"
        if ch in updates:
            save_payload[key] = bool(updates[ch])
        elif key in updates:
            save_payload[key] = bool(updates[key])

    if "whatsapp_monthly_limit" in updates:
        save_payload["whatsapp_monthly_limit"] = int(updates["whatsapp_monthly_limit"])
    if "whatsapp_used_conversations" in updates:
        save_payload["whatsapp_used_conversations"] = int(updates["whatsapp_used_conversations"])

    save_merchant_settings(tenant_id, save_payload)
    return get_merchant_channels(tenant_id, fresh=True)


def check_and_increment_whatsapp_usage(tenant_id: str, tenant_data: dict = None) -> tuple:
    """
    فحص رصيد محادثات الواتساب وزيادة العداد في حالة السماح.
    ترجع: (is_allowed: bool, used: int, limit: int)
    """
    channels = get_merchant_channels(tenant_id, tenant_data=tenant_data)
    limit = channels.get("whatsapp_monthly_limit", 500)
    used = channels.get("whatsapp_used_conversations", 0)

    # إذا كان الحد 0 أو سالب، يعتبر غير محدود (Unlimited)
    if limit > 0 and used >= limit:
        return False, used, limit

    # زيادة العداد
    new_used = used + 1
    save_merchant_settings(tenant_id, {"whatsapp_used_conversations": new_used})
    return True, new_used, limit
