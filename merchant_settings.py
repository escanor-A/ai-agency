import json
import os
import threading

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "merchant_settings.json")
_LOCK = threading.Lock()


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
    """حفظ إعدادات التاجر الإضافية"""
    if not tenant_id or not updates:
        return {}
    with _LOCK:
        data = _read_all()
        tid = str(tenant_id)
        if tid not in data:
            data[tid] = {}
        data[tid].update(updates)
        _write_all(data)
        return data[tid]


def get_merchant_google_sheets_url(tenant_id: str) -> str:
    """الحصول على رابط شيت المخزن للتاجر"""
    return load_merchant_settings(tenant_id).get("google_sheets_url", "")
