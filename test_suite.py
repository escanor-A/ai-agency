import hashlib
import hmac
import os
import time
import pytest

from check_stock import _norm_ar, escape_like
from customer_agent import RateLimiter
import orders
from orders import normalize_phone, normalize_text
import server
from server import verify_meta_signature
from telegram_bot import LINK_CODE_RE


# ============================================================================
# 1. اختبارات normalize_phone و normalize_text
# ============================================================================

def test_normalize_text():
    # همزات
    assert normalize_text("أحمد") == "احمد"
    assert normalize_text("إبراهيم") == "ابراهيم"
    assert normalize_text("آية") == "ايه"
    # تاء مربوطة
    assert normalize_text("القاهرة") == "القاهره"
    # ألف مقصورة
    assert normalize_text("منى") == "مني"
    # مسافات زائدة
    assert normalize_text("   الجيزة   ") == "الجيزه"


def test_normalize_phone():
    # أرقام مصرية صحيحة بمختلف الشبكات
    assert normalize_phone("01012345678") == "01012345678"
    assert normalize_phone("01123456789") == "01123456789"
    assert normalize_phone("01234567890") == "01234567890"
    assert normalize_phone("01512345678") == "01512345678"

    # أرقام مع كود مصر الدولي (+20 أو 20)
    assert normalize_phone("+201012345678") == "01012345678"
    assert normalize_phone("201112345678") == "01112345678"

    # أرقام بأرقام هندية/عربية
    assert normalize_phone("٠١٠١٢٣٤٥٦٧٨") == "01012345678"

    # مسافات أو فواصل
    assert normalize_phone("010 1234 5678") == "01012345678"
    assert normalize_phone("010-1234-5678") == "01012345678"

    # أرقام غير صالحة -> None
    assert normalize_phone("01312345678") is None  # أرضي أو كود شبكة غير صحيح
    assert normalize_phone("0101234567") is None   # 10 أرقام (ناقص)
    assert normalize_phone("010123456789") is None  # 12 رقم (زيادة)
    assert normalize_phone("abcdef") is None
    assert normalize_phone("") is None


# ============================================================================
# 2. اختبارات quote_order (نسبة، خصم ثابت، شحن مجاني، كود غلط، كود صح lowercase)
# ============================================================================

class MockTable:
    def __init__(self, data):
        self._data = data

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def in_(self, *args, **kwargs):
        return self

    def execute(self):
        class Result:
            pass
        res = Result()
        res.data = self._data
        return res


def test_quote_order_scenarios(monkeypatch):
    tenant_id = "test_tenant_123"

    # بيانات تجريبية:
    # الشحن: القاهرة 40 جنيه
    shipping_data = [{"governorate": "القاهرة", "price": 40.0}]

    # الأصناف: تيشيرت سعره 100 جنيه
    variants_data = [
        {
            "id": "var_1",
            "sku": "TS-BLK-L",
            "size": "L",
            "color": "أسود",
            "available": 10,
            "products": {"name": "تيشيرت أسود", "price": 100.0},
        }
    ]

    # العروض الترويجية المسجلة
    promotions_data = [
        {"code": "SAVE20", "promo_type": "percentage", "discount_value": 20, "description": "خصم 20%"},
        {"code": "OFF50", "promo_type": "fixed", "discount_value": 50, "description": "خصم 50 جنيه"},
        {"code": "FREESHIP", "promo_type": "free_shipping", "discount_value": 0, "description": "شحن مجاني"},
    ]

    def mock_table(table_name):
        if table_name == "shipping_rates":
            return MockTable(shipping_data)
        elif table_name == "variants":
            return MockTable(variants_data)
        elif table_name == "promotions":
            return MockTable(promotions_data)
        return MockTable([])

    monkeypatch.setattr(orders.supabase, "table", mock_table)

    items = ["TS-BLK-L:2"]  # subtotal = 200, fee = 40

    # أ) كود نسبة: SAVE20 (20% من 200 = 40)
    res_percent = orders.quote_order(tenant_id, items, "القاهرة", promo_code="SAVE20")
    assert res_percent["status"] == "ok"
    assert res_percent["subtotal"] == 200.0
    assert res_percent["discount"] == 40.0
    assert res_percent["applied_code"] == "SAVE20"
    assert res_percent["promo_error"] == ""
    assert "SAVE20" in res_percent["discount_note"]
    assert res_percent["total"] == 200.0  # 200 - 40 + 40

    # ب) كود خصم ثابت: OFF50 (50 جنيه)
    res_fixed = orders.quote_order(tenant_id, items, "القاهرة", promo_code="OFF50")
    assert res_fixed["status"] == "ok"
    assert res_fixed["discount"] == 50.0
    assert res_fixed["applied_code"] == "OFF50"
    assert res_fixed["promo_error"] == ""
    assert res_fixed["total"] == 190.0  # 200 - 50 + 40

    # ج) كود شحن مجاني: FREESHIP
    res_freeship = orders.quote_order(tenant_id, items, "القاهرة", promo_code="FREESHIP")
    assert res_freeship["status"] == "ok"
    assert res_freeship["shipping_fee"] == 0.0
    assert res_freeship["applied_code"] == "FREESHIP"
    assert res_freeship["promo_error"] == ""
    assert res_freeship["total"] == 200.0  # 200 - 0 + 0

    # د) كود غلط: WRONG_CODE
    res_wrong = orders.quote_order(tenant_id, items, "القاهرة", promo_code="WRONG_CODE")
    assert res_wrong["status"] == "ok"
    assert res_wrong["discount"] == 0.0
    assert res_wrong["applied_code"] is None
    assert "غير صالح أو منتهي" in res_wrong["promo_error"]
    assert "غير صالح" not in res_wrong["discount_note"]  # الخطأ لا يوضع في discount_note
    assert res_wrong["total"] == 240.0  # 200 + 40

    # هـ) كود صح بحروف صغيرة: save20
    res_lower = orders.quote_order(tenant_id, items, "القاهرة", promo_code="save20")
    assert res_lower["status"] == "ok"
    assert res_lower["discount"] == 40.0
    assert res_lower["applied_code"] == "SAVE20"
    assert res_lower["promo_error"] == ""


    # و) عرض تلقائي للشحن المجاني (بدون كود خصم، لقطعتين فأكثر)
    promotions_data.append({
        "code": None,
        "promo_type": "free_shipping",
        "min_order_items": 2,
        "discount_value": 0,
        "description": "عرض شحن مجاني تلقائي على قطعتين",
    })
    res_auto = orders.quote_order(tenant_id, items, "القاهرة", promo_code="")
    assert res_auto["status"] == "ok"
    assert res_auto["shipping_fee"] == 0.0
    assert "عرض شحن مجاني تلقائي" in res_auto["discount_note"]
    assert res_auto["total"] == 200.0  # 200 + 0


# ============================================================================
# 3. اختبارات LINK_CODE_RE
# ============================================================================

def test_link_code_re():
    # كود صالح: 16 حرف hex كبير متبوعاً بـ _ ثم 9 إلى 11 رقماً
    valid_code = "A1B2C3D4E5F67890_1730000000"
    assert LINK_CODE_RE.fullmatch(valid_code) is not None

    # يرفض علامة *
    assert LINK_CODE_RE.fullmatch("A1B2C3D4E5F6789*_1730000000") is None
    assert LINK_CODE_RE.fullmatch("*") is None

    # يرفض علامة %
    assert LINK_CODE_RE.fullmatch("A1B2C3D4E5F6789%_1730000000") is None
    assert LINK_CODE_RE.fullmatch("%") is None

    # يرفض a,b
    assert LINK_CODE_RE.fullmatch("a,b") is None
    assert LINK_CODE_RE.fullmatch("A1B2C3D4E5F67890_1730000000,extra") is None

    # يرفض الأحرف الصغيرة (لأن التوكن HEX بأحرف كبيرة)
    assert LINK_CODE_RE.fullmatch("a1b2c3d4e5f67890_1730000000") is None


# ============================================================================
# 4. اختبارات verify_meta_signature (ALLOW_UNSIGNED مفعل ومش مفعل)
# ============================================================================

def test_verify_meta_signature_allow_unsigned(monkeypatch):
    body = b'{"object":"page","entry":[]}'

    # 1. بدون سر وفي وضع ALLOW_UNSIGNED=False -> يجب أن يرفض
    monkeypatch.setattr(server, "META_APP_SECRET", "")
    monkeypatch.setattr(server, "ALLOW_UNSIGNED", False)
    assert verify_meta_signature(body, "") is False
    assert verify_meta_signature(body, "sha256=invalid") is False

    # 2. بدون سر وفي وضع ALLOW_UNSIGNED=True -> يقبل للتطوير المحلي
    monkeypatch.setattr(server, "META_APP_SECRET", "")
    monkeypatch.setattr(server, "ALLOW_UNSIGNED", True)
    assert verify_meta_signature(body, "") is True

    # 3. بوجود سر حقيقي: يرفض التوقيع الخطأ ويقبل التوقيع الصحيح
    secret = "my_meta_secret_key"
    monkeypatch.setattr(server, "META_APP_SECRET", secret)
    monkeypatch.setattr(server, "ALLOW_UNSIGNED", False)

    correct_hash = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    valid_header = f"sha256={correct_hash}"
    invalid_header = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

    assert verify_meta_signature(body, valid_header) is True
    assert verify_meta_signature(body, invalid_header) is False
    assert verify_meta_signature(body, "invalid_prefix_hash") is False
    assert verify_meta_signature(body, "") is False


# ============================================================================
# 5. اختبارات escape_like و _norm_ar
# ============================================================================

def test_escape_like():
    assert escape_like("cotton%100") == r"cotton\%100"
    assert escape_like("t_shirt") == r"t\_shirt"
    assert escape_like(r"path\to") == r"path\\to"
    assert escape_like(r"all_%_and_\_mix") == r"all\_\%\_and\_\\\_mix"
    assert escape_like("item*name") == "itemname"
    assert escape_like("*") == ""
    assert escape_like("***") == ""
    assert escape_like("") == ""
    assert escape_like(None) == ""


def test_norm_ar():
    assert _norm_ar("  أسود  ") == "اسود"
    assert _norm_ar("أبيض") == "ابيض"
    assert _norm_ar("إسود") == "اسود"
    assert _norm_ar("آخضر") == "اخضر"
    assert _norm_ar("كحلى") == "كحلي"
    assert _norm_ar("بدلة") == "بدله"
    assert _norm_ar("BLACK") == "black"
    assert _norm_ar("") == ""
    assert _norm_ar(None) == ""


# ============================================================================
# 6. اختبارات حد المعدل (Rate Limiting)
# ============================================================================

def test_rate_limiter_per_minute():
    limiter = RateLimiter(per_minute_limit=8)
    tenant_id = "tenant_test"
    sender_id = "sender_123"

    # أول 8 رسائل مقبولة
    for i in range(8):
        allowed, msg = limiter.check_rate_limit(tenant_id, sender_id)
        assert allowed is True, f"الرسالة رقم {i+1} كان المفروض تتقبل"
        assert msg == ""

    # الرسالة رقم 9 تُرفض مع رد مهذب
    allowed, msg = limiter.check_rate_limit(tenant_id, sender_id)
    assert allowed is False
    assert "الانتظار لحظات" in msg

    # الرسائل التالية خلال نفس الدقيقة تُرفض ويتم تجاهلها بدون رد مكرر
    for _ in range(5):
        allowed, msg = limiter.check_rate_limit(tenant_id, sender_id)
        assert allowed is False
        assert msg == ""

    # بعد مرور أكثر من دقيقة على التحذير السابق
    limiter.last_warned[(tenant_id, sender_id)] = time.time() - 65.0
    allowed, msg = limiter.check_rate_limit(tenant_id, sender_id)
    assert allowed is False
    assert "الانتظار لحظات" in msg


def test_rate_limiter_daily_cap(monkeypatch):
    monkeypatch.setenv("TENANT_DAILY_MESSAGE_CAP", "3")
    limiter = RateLimiter(per_minute_limit=8)
    tenant_id = "tenant_daily"

    # إرسال 3 رسائل من مرسلين مختلفين
    assert limiter.check_rate_limit(tenant_id, "user_1")[0] is True
    assert limiter.check_rate_limit(tenant_id, "user_2")[0] is True
    assert limiter.check_rate_limit(tenant_id, "user_3")[0] is True

    # الرسالة الرابعة تتجاوز السقف اليومي للتاجر
    allowed, msg = limiter.check_rate_limit(tenant_id, "user_4")
    assert allowed is False
    assert "للحد الأقصى" in msg

    # رسالة لاحقة تتجاهل
    allowed2, msg2 = limiter.check_rate_limit(tenant_id, "user_4")
    assert allowed2 is False
    assert msg2 == ""


def test_rate_limiter_cleanup():
    limiter = RateLimiter(per_minute_limit=8)
    # إضافة طوابع زمنية قديمة (منتهية الصلاحية)
    old_time = time.time() - 100.0
    limiter.sender_timestamps[("t1", "s1")] = [old_time]
    limiter.last_warned[("t1", "s1")] = old_time

    limiter.cleanup()
    # يجب أن يتم مسح المفتاح بعد التنظيف
    assert ("t1", "s1") not in limiter.sender_timestamps
    assert ("t1", "s1") not in limiter.last_warned


def test_rate_limiter_thread_safety():
    from concurrent.futures import ThreadPoolExecutor
    limiter = RateLimiter(per_minute_limit=100)
    tenant_id = "tenant_concurrency"

    def send_msg(i):
        return limiter.check_rate_limit(tenant_id, f"sender_{i % 5}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(send_msg, range(40)))

    assert len(results) == 40
    assert all(r[0] is True for r in results)


# ============================================================================
# 7. اختبارات التحقق من quote_order وحماية تكرار الطلبات (place_order)
# ============================================================================

def test_place_order_quote_verification_and_deduplication(monkeypatch):
    tenant_id = "tenant_test_quote"
    shipping_data = [{"governorate": "القاهرة", "price": 40.0}]
    variants_data = [
        {
            "id": "var_1",
            "sku": "TS-BLK-L",
            "size": "L",
            "color": "أسود",
            "available": 5,
            "products": {"name": "تيشيرت أسود", "price": 100.0},
        }
    ]

    def mock_table(table_name):
        if table_name == "shipping_rates":
            return MockTable(shipping_data)
        elif table_name == "variants":
            return MockTable(variants_data)
        elif table_name == "promotions":
            return MockTable([])
        return MockTable([])

    rpc_calls = []

    class MockRpc:
        def __init__(self, fn_name, params):
            self.fn_name = fn_name
            self.params = params

        def execute(self):
            rpc_calls.append({"fn": self.fn_name, "params": self.params})

            class Res:
                data = {"order_id": "ord_999", "order_number": 101, "total": 240.0}

            return Res()

    def mock_rpc(fn_name, params):
        return MockRpc(fn_name, params)

    monkeypatch.setattr(orders.supabase, "table", mock_table)
    monkeypatch.setattr(orders.supabase, "rpc", mock_rpc)

    session_id = "test_sess_abc"
    items = ["TS-BLK-L:2"]

    # 1. فحص load_items عند نقص الكمية: يجب ألا تذكر الرقم المتاح للعميل
    lines, err = orders.load_items(tenant_id, ["TS-BLK-L:10"])
    assert lines is None
    assert "غير متوفرة" in err
    assert "5" not in err

    # 2. محاولة place_order قبل quote_order في نفس الجلسة -> تفشل
    orders.SESSION_QUOTES.clear()
    res_fail = orders.place_order(
        tenant_id, "أحمد علي", "01012345678", "القاهرة", "شارع التحرير، الدقي، شقة 4", items, session_id=session_id
    )
    assert res_fail["status"] == "error"
    assert "quote_order" in res_fail["reason"]

    # 3. حساب quote_order في الجلسة بنجاح
    q = orders.quote_order(tenant_id, items, "القاهرة", session_id=session_id)
    assert q["status"] == "ok"
    assert "quote_id" in q
    assert orders.has_valid_quote(session_id, items, "القاهرة") is True

    # محاولة place_order بمحافظة مختلفة -> تفشل
    res_gov_mismatch = orders.place_order(
        tenant_id, "أحمد علي", "01012345678", "الجيزة", "شارع التحرير، الدقي، شقة 4", items, session_id=session_id
    )
    assert res_gov_mismatch["status"] == "error"

    # محاولة place_order بأصناف مختلفة -> تفشل
    res_item_mismatch = orders.place_order(
        tenant_id, "أحمد علي", "01012345678", "القاهرة", "شارع التحرير، الدقي، شقة 4", ["TS-BLK-L:1"], session_id=session_id
    )
    assert res_item_mismatch["status"] == "error"

    # 4. place_order بالبيانات المطابقة -> تنجح عبر place_order_v3
    res_success = orders.place_order(
        tenant_id, "أحمد علي", "01012345678", "القاهرة", "شارع التحرير، الدقي، شقة 4", items, session_id=session_id
    )
    assert res_success["status"] == "ok"
    assert res_success["order_number"] == 101
    assert len(rpc_calls) == 1
    assert rpc_calls[0]["fn"] == "place_order_v3"
    assert "p_idempotency_key" in rpc_calls[0]["params"]
    assert rpc_calls[0]["params"]["p_idempotency_key"] == q["quote_id"]


# ============================================================================
# 8. اختبارات resolve_tenant_strict
# ============================================================================

def test_resolve_tenant_strict(monkeypatch):
    import telegram_bot
    from telegram_bot import resolve_tenant_strict

    fake_tenants = [
        {"id": "11111111-aaaa-bbbb-cccc-000000000001", "name": "محل النيل", "bot_enabled": True},
        {"id": "22222222-aaaa-bbbb-cccc-000000000002", "name": "محل الأهرام", "bot_enabled": True},
        {"id": "22222222-xxxx-yyyy-zzzz-000000000003", "name": "محل فرع تاني", "bot_enabled": True},
    ]

    class FakeTable:
        def select(self, *args, **kwargs):
            return self

        def execute(self):
            class Res:
                data = fake_tenants
            return Res()

    monkeypatch.setattr(telegram_bot.supabase, "table", lambda t: FakeTable())

    # 1. UUID كامل مطابق
    t, err = resolve_tenant_strict("11111111-aaaa-bbbb-cccc-000000000001")
    assert err is None
    assert t["name"] == "محل النيل"

    # 2. أول 8 حروف فريدة مطابقة
    t, err = resolve_tenant_strict("11111111")
    assert err is None
    assert t["name"] == "محل النيل"

    # 3. أول 8 حروف غير فريدة (أكثر من تاجر يطابق) -> يجب أن يرفض
    t, err = resolve_tenant_strict("22222222")
    assert t is None
    assert "أكثر من تاجر" in err

    # 4. إدخال أقل من 8 خانات -> مرفوض
    t, err = resolve_tenant_strict("1111111")
    assert t is None
    assert "غير صالح" in err

    # 5. إدخال اسم أو حروف غير هيكس -> مرفوض
    t, err = resolve_tenant_strict("محل النيل")
    assert t is None
    assert "غير صالح" in err

    # 6. معرّف 8 حروف غير موجود
    t, err = resolve_tenant_strict("33333333")
    assert t is None
    assert "لم يتم العثور" in err


# ============================================================================
# 9. اختبارات إضافة وتحديث الأصناف (variant upsert و SKU اللاتيني)
# ============================================================================

def test_variant_upsert_and_stock_movement(monkeypatch):
    import re
    import telegram_bot
    from telegram_bot import generate_sku, process_confirmed_proposals

    # 1. فحص كود الصنف SKU اللاتيني
    for _ in range(20):
        sku = generate_sku()
        assert re.fullmatch(r"^P[A-Z]{6}$", sku) is not None

    tenant_id = "tenant_test_upsert"

    # جداول محاكاة في الذاكرة
    mock_db = {
        "products": [
            {"id": "prod_1", "tenant_id": tenant_id, "name": "تيشيرت صيفي", "price": 150.0, "active": True}
        ],
        "variants": [
            {
                "id": "var_1",
                "tenant_id": tenant_id,
                "product_id": "prod_1",
                "sku": "PABCDEF",
                "size": "L",
                "color": "أسود",
                "stock": 5,
            }
        ],
    }
    rpc_movements = []

    class MockSupabaseTable:
        def __init__(self, table_name):
            self.table_name = table_name
            self.conditions = {}

        def select(self, *args, **kwargs):
            return self

        def eq(self, col, val):
            self.conditions[col] = val
            return self

        def insert(self, payload):
            if isinstance(payload, dict):
                data = dict(payload)
                if "id" not in data:
                    data["id"] = f"{self.table_name}_{len(mock_db.get(self.table_name, [])) + 1}"
                mock_db.setdefault(self.table_name, []).append(data)
                self._last_inserted = [data]
            return self

        def execute(self):
            class Res:
                pass
            res = Res()
            if hasattr(self, "_last_inserted"):
                res.data = self._last_inserted
                return res

            items = mock_db.get(self.table_name, [])
            filtered = []
            for item in items:
                match = True
                for col, val in self.conditions.items():
                    if item.get(col) != val:
                        match = False
                        break
                if match:
                    filtered.append(item)
            res.data = filtered
            return res

    class MockSupabaseRpc:
        def __init__(self, name, params):
            self.name = name
            self.params = params

        def execute(self):
            rpc_movements.append(self.params)
            class Res:
                data = True
            return Res()

    monkeypatch.setattr(telegram_bot.supabase, "table", lambda t: MockSupabaseTable(t))
    monkeypatch.setattr(telegram_bot.supabase, "rpc", lambda n, p: MockSupabaseRpc(n, p))

    # أ) إضافة صنف موجود مسبقاً بنفس (product_id, size, _norm_ar(color))
    # يجب ألا ينشئ variant جديد، بل يزيد الكمية عبر apply_stock_movement
    item_existing = {
        "type": "new_product",
        "name": "تيشيرت صيفي",
        "price": 150.0,
        "size": "L",
        "color": "اسود",  # همزة مطبعة بـ _norm_ar
        "qty": 4,
    }

    report = process_confirmed_proposals(tenant_id, "pid1", [item_existing])
    assert report["success_count"] == 1
    assert report["failure_count"] == 0
    assert len(mock_db["variants"]) == 1  # لم يتم إنشاء صنف مكرر
    assert len(rpc_movements) == 1
    assert rpc_movements[0]["p_change"] == 4
    assert rpc_movements[0]["p_reason"] == "restock"
    assert rpc_movements[0]["p_variant"] == "var_1"

    # ب) إضافة صنف لمقاس/لون جديد في منتج موجود، ولكن بسعر مختلف
    # يجب أن ينشئ variant بـ stock=0 ثم حركة restock، وينبه التاجر لاختلاف السعر
    item_new_variant = {
        "type": "new_product",
        "name": "تيشيرت صيفي",
        "price": 180.0,  # سعر مختلف عن المسجل (150)
        "size": "M",
        "color": "أبيض",
        "qty": 10,
    }

    report2 = process_confirmed_proposals(tenant_id, "pid2", [item_new_variant])
    assert report2["success_count"] == 1
    assert len(report2["warnings"]) == 1
    assert "سعر" in report2["warnings"][0]
    assert len(mock_db["variants"]) == 2
    new_var = mock_db["variants"][1]
    assert new_var["size"] == "M"
    assert new_var["stock"] == 0
    assert re.fullmatch(r"^P[A-Z]{6}$", new_var["sku"]) is not None
    assert len(rpc_movements) == 2
    assert rpc_movements[1]["p_change"] == 10
    assert rpc_movements[1]["p_reason"] == "restock"


# ============================================================================
# 10. اختبار ربط الإجمالي بالتسعيرة (rejecting mismatch)
# ============================================================================

def test_place_order_total_mismatch(monkeypatch):
    tenant_id = "tenant_mismatch"
    shipping_data = [{"governorate": "القاهرة", "price": 40.0}]
    variants_data = [
        {
            "id": "var_1",
            "sku": "TS-BLK-L",
            "size": "L",
            "color": "أسود",
            "available": 5,
            "products": {"name": "تيشيرت أسود", "price": 100.0},
        }
    ]

    def mock_table(table_name):
        if table_name == "shipping_rates":
            return MockTable(shipping_data)
        elif table_name == "variants":
            return MockTable(variants_data)
        elif table_name == "promotions":
            return MockTable([])
        return MockTable([])

    monkeypatch.setattr(orders.supabase, "table", mock_table)

    session_id = "sess_mismatch"
    items = ["TS-BLK-L:1"]

    # 1. تسعير صحيح
    q = orders.quote_order(tenant_id, items, "القاهرة", session_id=session_id)
    assert q["status"] == "ok"

    # 2. التلاعب في الإجمالي المسجل بالجلسة ليختلف عن الحقيقي بأكثر من 0.01
    with orders.SESSION_QUOTES_LOCK:
        orders.SESSION_QUOTES[session_id][0]["total"] = 50.0  # الإجمالي الفعلي 140.0

    res = orders.place_order(
        tenant_id, "عميل تجريبي", "01012345678", "القاهرة", "شارع التحرير، الدقي، شقة 4", items, session_id=session_id
    )
    assert res["status"] == "error"
    assert res["reason"] == "order_total_mismatch"


# ============================================================================
# 11. اختبار حماية التكرار Idempotency (نفس quote_id مرتين = أوردر واحد بدون إشعار مكرر)
# ============================================================================

def test_place_order_idempotency_v3(monkeypatch):
    tenant_id = "tenant_idem"
    shipping_data = [{"governorate": "القاهرة", "price": 40.0}]
    variants_data = [
        {
            "id": "var_1",
            "sku": "TS-BLK-L",
            "size": "L",
            "color": "أسود",
            "available": 5,
            "products": {"name": "تيشيرت أسود", "price": 100.0},
        }
    ]

    notifications_sent = []

    def mock_notify(*args, **kwargs):
        notifications_sent.append(args)

    class MockRpcIdempotent:
        call_count = 0

        def __init__(self, fn_name, params):
            self.fn_name = fn_name
            self.params = params

        def execute(self):
            MockRpcIdempotent.call_count += 1
            is_dup = MockRpcIdempotent.call_count > 1

            class Res:
                data = {
                    "order_id": "ord_idem_1",
                    "order_number": 999,
                    "total": 140.0,
                    "duplicate": is_dup,
                }

            return Res()

    def mock_table(table_name):
        if table_name == "shipping_rates":
            return MockTable(shipping_data)
        elif table_name == "variants":
            return MockTable(variants_data)
        elif table_name == "promotions":
            return MockTable([])
        return MockTable([])

    monkeypatch.setattr(orders.supabase, "table", mock_table)
    monkeypatch.setattr(orders.supabase, "rpc", lambda fn, p: MockRpcIdempotent(fn, p))
    monkeypatch.setattr(orders.notify, "send_order_notification", mock_notify)

    session_id = "sess_idem"
    items = ["TS-BLK-L:1"]

    # تسعير
    orders.quote_order(tenant_id, items, "القاهرة", session_id=session_id)

    # الطلب الأول: ينشئ أوردر ويرسل إشعاراً
    res1 = orders.place_order(
        tenant_id, "عميل أول", "01012345678", "القاهرة", "شارع التحرير، الدقي، شقة 4", items, session_id=session_id
    )
    assert res1["status"] == "ok"
    assert res1["order_number"] == 999
    assert len(notifications_sent) == 1

    # الطلب الثاني بنفس quote_id: يرجع نفس الأوردر دون إشعار تاجر جديد
    res2 = orders.place_order(
        tenant_id, "عميل أول", "01012345678", "القاهرة", "شارع التحرير، الدقي، شقة 4", items, session_id=session_id
    )
    assert res2["status"] == "ok"
    assert res2["order_number"] == 999
    assert len(notifications_sent) == 1


# ============================================================================
# 12. اختبار ملكية الأوردر (cancel/update من عميل غير المالك ومن أوردر من غير customer_id و Fail-closed)
# ============================================================================

def test_order_ownership_and_fail_closed(monkeypatch):
    import customer_agent

    tenant_id = "t_own"

    db_orders = {
        10: {
            "id": "ord_10",
            "order_number": 10,
            "tenant_id": tenant_id,
            "status": "new",
            "customer_id": "cust_owner_uuid",
            "phone": "01011112222",
            "customer_name": "أحمد المالك",
        },
        20: {
            "id": "ord_20",
            "order_number": 20,
            "tenant_id": tenant_id,
            "status": "confirmed",
            "customer_id": None,
            "phone": "01033334444",
            "customer_name": "ضيف بدون حساب",
        },
    }

    class MockOrdersTable:
        def __init__(self):
            self._eqs = {}

        def select(self, *args, **kwargs):
            return self

        def eq(self, col, val):
            self._eqs[col] = val
            return self

        def execute(self):
            num = self._eqs.get("order_number")
            o = db_orders.get(num)
            res = MockTable([])
            res.data = [dict(o)] if o and self._eqs.get("tenant_id") == o["tenant_id"] else []
            return res

    monkeypatch.setattr(customer_agent.supabase, "table", lambda t: MockOrdersTable() if t == "orders" else MockTable([]))
    monkeypatch.setattr(orders, "cancel_order", lambda t, o, reason="": {"status": "ok", "cancelled": o})
    monkeypatch.setattr(orders, "update_order_shipping", lambda t, o, phone="", address="": {"status": "ok", "updated": o})

    # 1. أوردر مرتبط بـ customer_id:
    # أ) غير المالك يحاول الإلغاء -> يرفض
    monkeypatch.setattr(orders, "get_or_create_customer", lambda *a, **k: "attacker_uuid")
    res_cancel_att = customer_agent.cancel_customer_order_handler(tenant_id, "messenger", "att_id", 10)
    assert res_cancel_att["status"] == "error"
    assert "مش مسجل بنفس هذا الحساب" in res_cancel_att["reason"]

    # ب) غير المالك يحاول التعديل -> يرفض
    res_update_att = customer_agent.update_order_address_or_phone_handler(
        tenant_id, "messenger", "att_id", 10, address="عنوان جديد للتسليم"
    )
    assert res_update_att["status"] == "error"
    assert "مش مسجل بنفس هذا الحساب" in res_update_att["reason"]

    # ج) Fail-closed لو customer_uuid = None -> يرفض
    monkeypatch.setattr(orders, "get_or_create_customer", lambda *a, **k: None)
    res_cancel_none = customer_agent.cancel_customer_order_handler(tenant_id, "messenger", "none_id", 10)
    assert res_cancel_none["status"] == "error"
    assert "مش مسجل بنفس هذا الحساب" in res_cancel_none["reason"]

    res_update_none = customer_agent.update_order_address_or_phone_handler(
        tenant_id, "messenger", "none_id", 10, address="عنوان جديد"
    )
    assert res_update_none["status"] == "error"
    assert "مش مسجل بنفس هذا الحساب" in res_update_none["reason"]

    # د) المالك الحقيقي -> ينجح
    monkeypatch.setattr(orders, "get_or_create_customer", lambda *a, **k: "cust_owner_uuid")
    res_cancel_owner = customer_agent.cancel_customer_order_handler(tenant_id, "messenger", "owner_id", 10)
    assert res_cancel_owner["status"] == "ok"

    res_update_owner = customer_agent.update_order_address_or_phone_handler(
        tenant_id, "messenger", "owner_id", 10, address="عنوان صحيح جديد"
    )
    assert res_update_owner["status"] == "ok"

    # 2. أوردر من غير customer_id (order 20):
    # أ) تعديل برقم موبايل حالي غير مطابق -> يرفض
    res_up_wrong_ph = customer_agent.update_order_address_or_phone_handler(
        tenant_id, "messenger", "guest_id", 20, address="عنوان جديد", current_phone="01099990000"
    )
    assert res_up_wrong_ph["status"] == "error"
    assert "غير مطابق" in res_up_wrong_ph["reason"]

    # ب) تعديل برقم موبايل حالي مطابق -> ينجح
    res_up_ok_ph = customer_agent.update_order_address_or_phone_handler(
        tenant_id, "messenger", "guest_id", 20, address="عنوان جديد", current_phone="01033334444"
    )
    assert res_up_ok_ph["status"] == "ok"

    # ج) إلغاء برقم موبايل غير مطابق -> يرفض
    res_can_wrong_ph = customer_agent.cancel_customer_order_handler(
        tenant_id, "messenger", "guest_id", 20, phone="01099990000"
    )
    assert res_can_wrong_ph["status"] == "error"
    assert "غير مطابق" in res_can_wrong_ph["reason"]

    # د) إلغاء برقم موبايل مطابق -> ينجح
    res_can_ok_ph = customer_agent.cancel_customer_order_handler(
        tenant_id, "messenger", "guest_id", 20, phone="01033334444"
    )
    assert res_can_ok_ph["status"] == "ok"


# ============================================================================
# 13. اختبار تقرير on_button بنص عادي وبدون parse_mode ورسالة عامة عند الفشل
# ============================================================================

@pytest.mark.anyio
async def test_on_button_plain_text_report(monkeypatch):
    import telegram_bot

    tenant_id = "t_btn"

    monkeypatch.setattr(telegram_bot, "get_owner_tenant", lambda cid: {"id": tenant_id, "name": "محل الاختبار"})
    monkeypatch.setattr(
        telegram_bot,
        "pop_pending_operation",
        lambda ctx, pid: {"chat_id": 12345, "tenant_id": tenant_id, "items": [{"type": "new_product"}]},
    )

    # تقرير فيه نجاح وفشل
    mock_report = {
        "success_count": 1,
        "failure_count": 1,
        "successes": ["تيشيرت أسود L (كمية: 5)"],
        "failures": ["DB constraint failure internal sql trace 123"],
        "warnings": [],
    }
    monkeypatch.setattr(telegram_bot, "process_confirmed_proposals", lambda tid, pid, items: mock_report)

    edited_messages = []

    class MockQuery:
        data = "ok:pid_test_123"

        def __init__(self):
            class Msg:
                chat = type("Chat", (), {"id": 12345})()
            self.message = Msg()

        async def answer(self):
            pass

        async def edit_message_text(self, text, **kwargs):
            edited_messages.append({"text": text, "kwargs": kwargs})

    class MockUpdate:
        callback_query = MockQuery()

    update = MockUpdate()
    context = type("Context", (), {"bot_data": {}})()

    await telegram_bot.on_button(update, context)

    assert len(edited_messages) == 1
    call = edited_messages[0]

    # لا يوجد parse_mode (نص عادي)
    assert "parse_mode" not in call["kwargs"] or call["kwargs"].get("parse_mode") is None

    # تفاصيل الخطأ الداخلي غير معروضة في الرسالة
    assert "internal sql trace" not in call["text"]
    assert "DB constraint failure" not in call["text"]

    # رسالة عامة للتاجر عن الفشل
    assert "تعذر تنفيذ بعض العمليات" in call["text"]
    assert "الناجح: 1 عملية" in call["text"]
    assert "الفاشل: 1 عملية" in call["text"]


# ============================================================================
# 14. اختبارات لوحة تحكم التاجر (Merchant Dashboard / Telegram Web App)
# ============================================================================

def test_dashboard_html_serving():
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)
    
    # فحص مسار /dashboard العام
    res = client.get("/dashboard")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "telegram-web-app.js" in res.text
    assert "tailwindcss" in res.text
    assert "لوحة تحكم التاجر" in res.text

    # فحص مسار /dashboard/{tenant_id}
    res_id = client.get("/dashboard/test-uuid-123")
    assert res_id.status_code == 200
    assert "telegram-web-app.js" in res_id.text


def test_dashboard_api_get_and_update(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import server
    import merchant_settings

    # توجيه ملف الإعدادات إلى مجلد مؤقت
    monkeypatch.setattr(merchant_settings, "SETTINGS_FILE", str(tmp_path / "test_settings.json"))

    mock_db = {
        "tenants": [
            {
                "id": "11111111-2222-3333-4444-555555555555",
                "name": "محل الأزياء العصرية",
                "bot_enabled": True,
                "facebook_page_id": "100200300",
                "facebook_page_token": "EAA_test_token",
                "telegram_chat_id": 987654321,
                "google_sheets_url": None,
            }
        ],
        "products": [{"id": "p1", "tenant_id": "11111111-2222-3333-4444-555555555555"}],
        "orders": [{"id": "o1", "tenant_id": "11111111-2222-3333-4444-555555555555"}],
        "conversations": [{"id": "c1", "tenant_id": "11111111-2222-3333-4444-555555555555"}],
        "messages": [{"id": "m1", "conversation_id": "c1"}],
    }

    class MockQueryBuilder:
        def __init__(self, table_name):
            self.table_name = table_name
            self._filters = []
            self._updates = None
            self._is_count = False

        def select(self, cols="*", count=None):
            if count:
                self._is_count = True
            return self

        def eq(self, col, val):
            self._filters.append((col, "eq", str(val)))
            return self

        def in_(self, col, vals):
            self._filters.append((col, "in", set(vals)))
            return self

        def order(self, col):
            return self

        def update(self, payload):
            self._updates = payload
            return self

        def execute(self):
            rows = mock_db.get(self.table_name, [])
            filtered = []
            for r in rows:
                match = True
                for col, op, val in self._filters:
                    r_val = str(r.get(col, ""))
                    if op == "eq" and r_val != val:
                        match = False
                        break
                    elif op == "in" and r.get(col) not in val:
                        match = False
                        break
                if match:
                    filtered.append(r)

            if self._updates is not None:
                for r in filtered:
                    r.update(self._updates)
                return type("Res", (), {"data": filtered})()

            count_val = len(filtered) if self._is_count else None
            return type("Res", (), {"data": filtered, "count": count_val})()

    monkeypatch.setattr(server.supabase, "table", lambda t: MockQueryBuilder(t))

    client = TestClient(server.app)

    # 1. اختبار GET /api/dashboard/{tenant_id}
    tid = "11111111-2222-3333-4444-555555555555"
    res = client.get(f"/api/dashboard/{tid}")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["name"] == "محل الأزياء العصرية"
    assert data["bot_enabled"] is True
    assert data["facebook_page_id"] == "100200300"
    assert data["stats"]["products_count"] == 1
    assert data["stats"]["orders_count"] == 1
    assert data["stats"]["messages_count"] == 1

    # 2. اختبار GET عبر البادئة (8 حروف)
    res_prefix = client.get(f"/api/dashboard/11111111")
    assert res_prefix.status_code == 200
    assert res_prefix.json()["data"]["id"] == tid

    # 3. اختبار POST لتحديث البيانات (فيسبوك وشيت المخزن)
    new_sheet = "https://docs.google.com/spreadsheets/d/my-new-sheet"
    post_res = client.post(
        f"/api/dashboard/{tid}",
        json={
            "facebook_page_id": "999888777",
            "facebook_page_token": "EAA_NEW_TOKEN",
            "google_sheets_url": new_sheet,
        }
    )
    assert post_res.status_code == 200
    updated_data = post_res.json()["data"]
    assert updated_data["facebook_page_id"] == "999888777"
    assert updated_data["facebook_page_token"] == "EAA_NEW_TOKEN"
    assert updated_data["google_sheets_url"] == new_sheet

    # 4. اختبار زر التبديل السريع للبوت toggle-bot
    toggle_res = client.post(f"/api/dashboard/{tid}/toggle-bot")
    assert toggle_res.status_code == 200
    assert toggle_res.json()["bot_enabled"] is False

    # إعادة التفعيل
    toggle_res2 = client.post(f"/api/dashboard/{tid}/toggle-bot", json={"bot_enabled": True})
    assert toggle_res2.status_code == 200
    assert toggle_res2.json()["bot_enabled"] is True

    # 5. اختبار الاستعلام عبر Telegram Chat ID
    tg_res = client.get("/api/dashboard/by-telegram/987654321")
    assert tg_res.status_code == 200
    assert tg_res.json()["data"]["name"] == "محل الأزياء العصرية"


# ============================================================================
# 15. اختبارات فرز التعليقات (Comment Triage) وسياسات الرد
# ============================================================================

def test_comment_triage_classification():
    from customer_agent import classify_comment

    # 1. مكان وعنوان
    assert classify_comment("فين المكان؟") == "location"
    assert classify_comment("العنوان فين يا فندم") == "location"

    # 2. مواعيد العمل
    assert classify_comment("مواعيد العمل لحد الساعة كام؟") == "hours"
    assert classify_comment("فاتحين الجمعة؟") == "hours"

    # 3. أسعار
    assert classify_comment("ده بكام؟") == "price"
    assert classify_comment("السعر كام؟") == "price"

    # 4. توفر ومقاسات
    assert classify_comment("المقاس ده متاح؟") == "availability"
    assert classify_comment("متوفر منه أسود لارج؟") == "availability"

    # 5. شحن وتوصيل
    assert classify_comment("في شحن لأسيوط؟") == "shipping"
    assert classify_comment("الشحن بكام للمحافظات؟") == "shipping"

    # 6. عروض
    assert classify_comment("القطعتين ولا واحدة في العرض؟") == "offer"
    assert classify_comment("في عروض دلوقتي أو خصم؟") == "offer"

    # 7. طلب شراء
    assert classify_comment("عايز أطلب تيشيرت أسود لارج") == "order"
    assert classify_comment("احجزلي قطعتين لو سمحت") == "order"

    # 8. مدح وشكر
    assert classify_comment("حاجتكم تحفة تسلموا") == "praise"
    assert classify_comment("الخامة روعة بجد ما شاء الله") == "praise"

    # 9. شكوى
    assert classify_comment("الأوردر اتأخر ومحدش بيرد عليا خالص!") == "complaint"
    assert classify_comment("الخامة وحشة جداً وعايز أعمل شكوى") == "complaint"

    # 10. تجاهل (skip)
    assert classify_comment("@Ahmed @Mohamed") == "skip"
    assert classify_comment("up") == "skip"


@pytest.mark.anyio
async def test_comment_triage_filters_and_policies(monkeypatch):
    import server

    tenant_id = "t_triage_123"
    page_id = "11223344"
    page_token = "EAAB_TEST_TOKEN"

    # تجهيز سجل التاجر
    monkeypatch.setattr(
        server.supabase,
        "table",
        lambda t: type(
            "QB",
            (),
            {
                "select": lambda self, *a, **k: self,
                "eq": lambda self, *a, **k: self,
                "execute": lambda self: type(
                    "Res",
                    (),
                    {
                        "data": [
                            {
                                "id": tenant_id,
                                "name": "براند النجوم",
                                "bot_enabled": True,
                                "facebook_page_token": page_token,
                            }
                        ]
                    },
                )(),
            },
        )(),
    )

    public_replies = []
    meta_replies = []
    complaints = []
    praises = []

    async def mock_public(comment_id, text, token):
        public_replies.append({"comment_id": comment_id, "text": text})

    async def mock_meta(sender_id, text, token, platform="messenger", comment_id=None):
        meta_replies.append({"sender_id": sender_id, "text": text, "comment_id": comment_id})

    def mock_complaint(tid, customer_name, comment_text, **kwargs):
        complaints.append({"tid": tid, "customer": customer_name, "text": comment_text})

    def mock_praise(tid, customer_name, feedback, **kwargs):
        praises.append({"tid": tid, "customer": customer_name, "text": feedback})

    async def mock_fetch_post(pid, tok):
        return "بوست عرض الصيف تيشيرتات قطن 100%"

    async def mock_fetch_img(v, cid, tok):
        return (None, "image/jpeg")

    async def mock_is_parent_false(pid, pg, tok):
        return False

    async def mock_has_replied_true(cid, pg, tok):
        return True

    async def mock_has_replied_false(cid, pg, tok):
        return False

    monkeypatch.setattr(server, "send_public_comment_reply", mock_public)
    monkeypatch.setattr(server, "send_meta_reply", mock_meta)
    monkeypatch.setattr(server, "send_complaint_notification", mock_complaint)
    monkeypatch.setattr(server, "submit_praise_review", mock_praise)
    monkeypatch.setattr(server, "fetch_post_text", mock_fetch_post)
    monkeypatch.setattr(server, "fetch_comment_image", mock_fetch_img)
    monkeypatch.setattr(server, "is_parent_comment_from_page", mock_is_parent_false)
    monkeypatch.setattr(server, "has_page_already_replied", mock_has_replied_false)

    # 1. اختبار شرط 1: تجاهل لو from.id فاضي أو للصفحة نفسها
    payload_self = {
        "entry": [
            {
                "id": page_id,
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "comment_id": "c_self",
                            "post_id": "p1",
                            "from": {"id": page_id, "name": "صفحة البراند"},
                            "message": "شكراً لكم",
                        },
                    }
                ],
            }
        ]
    }
    await server.process_meta_event(payload_self)
    assert len(public_replies) == 0
    assert len(meta_replies) == 0

    # 2. اختبار شرط 1: رد عميل على تعليق عميل آخر (parent_id != post_id ولم تكتبه الصفحة) -> تجاهل
    monkeypatch.setattr(server, "is_parent_comment_from_page", mock_is_parent_false)
    payload_subcomment = {
        "entry": [
            {
                "id": page_id,
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "comment_id": "c_sub",
                            "post_id": "p1",
                            "parent_id": "other_cust_comment",
                            "from": {"id": "cust_99", "name": "عميل 99"},
                            "message": "بكام ده؟",
                        },
                    }
                ],
            }
        ]
    }
    await server.process_meta_event(payload_subcomment)
    assert len(public_replies) == 0
    assert len(meta_replies) == 0

    # 3. اختبار شرط 7: فحص أن الصفحة لم ترد مسبقاً (لو ردت مسبقاً -> تجاهل)
    monkeypatch.setattr(server, "has_page_already_replied", mock_has_replied_true)
    payload_already = {
        "entry": [
            {
                "id": page_id,
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "comment_id": "c_already",
                            "post_id": "p1",
                            "parent_id": "p1",
                            "from": {"id": "cust_1", "name": "أحمد"},
                            "message": "فين المكان؟",
                        },
                    }
                ],
            }
        ]
    }
    await server.process_meta_event(payload_already)
    assert len(public_replies) == 0

    # الآن نفعل استقبال الردود العادية (الصفحة لم ترد بعد والتعليق أساسي)
    monkeypatch.setattr(server, "has_page_already_replied", mock_has_replied_false)

    # 4. اختبار شكوى (complaint): إشعار التاجر على تليجرام وبدون رد علني!
    payload_complaint = {
        "entry": [
            {
                "id": page_id,
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "comment_id": "c_comp_1",
                            "post_id": "p1",
                            "parent_id": "p1",
                            "from": {"id": "cust_angry", "name": "سيد"},
                            "message": "الأوردر اتأخر ومحدش بيرد عليا خالص!",
                        },
                    }
                ],
            }
        ]
    }
    await server.process_meta_event(payload_complaint)
    assert len(complaints) == 1
    assert complaints[0]["customer"] == "سيد"
    assert len(public_replies) == 0  # بدون رد علني تماماً!

    # 5. اختبار مدح (praise): شكر علني + تسجيل ريفيو
    payload_praise = {
        "entry": [
            {
                "id": page_id,
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "comment_id": "c_praise_1",
                            "post_id": "p1",
                            "parent_id": "p1",
                            "from": {"id": "cust_happy", "name": "مريم"},
                            "message": "حاجتكم تحفة تسلموا",
                        },
                    }
                ],
            }
        ]
    }
    await server.process_meta_event(payload_praise)
    assert len(praises) == 1
    assert praises[0]["customer"] == "مريم"
    assert len(public_replies) == 1
    assert "شكراً" in public_replies[-1]["text"]

    # 6. اختبار سعر (price): رد علني قصير + دعوة للخاص (وردم خاص Private Reply)
    payload_price = {
        "entry": [
            {
                "id": page_id,
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "comment_id": "c_price_1",
                            "post_id": "p1",
                            "parent_id": "p1",
                            "from": {"id": "cust_price", "name": "طارق"},
                            "message": "ده بكام؟",
                        },
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(server, "handle_customer_message", lambda *a, **k: "سعر التيشيرت 350 جنيه ومتاح مقاسات M و L.")
    await server.process_meta_event(payload_price)
    assert len(public_replies) == 2
    assert "الخاص" in public_replies[-1]["text"] or "DM" in public_replies[-1]["text"]
    assert len(meta_replies) == 1
    assert meta_replies[-1]["comment_id"] == "c_price_1"

    # 7. اختبار عنوان أو شحن أو عرض: رد علني بالإجابة مباشرة
    payload_shipping = {
        "entry": [
            {
                "id": page_id,
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "comment_id": "c_ship_1",
                            "post_id": "p1",
                            "parent_id": "p1",
                            "from": {"id": "cust_ship", "name": "كريم"},
                            "message": "في شحن لأسيوط؟",
                        },
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(server, "handle_customer_message", lambda *a, **k: "أيوة متاح شحن لأسيوط بـ 60 جنيه والتوصيل خلال يومين عمل!")
    await server.process_meta_event(payload_shipping)
    assert len(public_replies) == 3
    assert "أسيوط" in public_replies[-1]["text"]


def test_shipping_rates_tool_and_tenant_settings(monkeypatch):
    import customer_agent

    tid = "t_settings_99"

    # 1. اختبار جلب سياسات المحل من tenant_settings
    mock_settings_row = {
        "working_hours": "من 12 ظهراً حتى 12 ليلاً",
        "inspection_policy": "المعاينة مجانية قبل دفع أي قرش",
        "return_policy": "استرجاع خلال 30 يوم",
    }
    monkeypatch.setattr(
        customer_agent.supabase,
        "table",
        lambda t: type(
            "QB",
            (),
            {
                "select": lambda self, *a, **k: self,
                "eq": lambda self, *a, **k: self,
                "execute": lambda self: type("Res", (), {"data": [mock_settings_row] if t == "tenant_settings" else []})(),
            },
        )(),
    )

    settings = customer_agent.get_tenant_settings(tid)
    assert settings["working_hours"] == "من 12 ظهراً حتى 12 ليلاً"
    assert settings["inspection_policy"] == "المعاينة مجانية قبل دفع أي قرش"

    sys_inst = customer_agent.build_system_instruction(tid, allow_orders=True)
    assert "من 12 ظهراً حتى 12 ليلاً" in sys_inst
    assert "المعاينة مجانية قبل دفع أي قرش" in sys_inst

    # 2. اختبار سقف التعليقات المنفصل
    limiter = customer_agent.RateLimiter()
    # استهلاك 3 تعليقات
    for i in range(3):
        allowed, _ = limiter.check_comment_rate_limit(tid, f"sender_{i}")
        assert allowed is True

    # التأكد من أن سقف المحادثات DMs لم يتأثر وظل 0
    assert limiter.tenant_daily_counts.get(tid) is None
    assert limiter.tenant_daily_comments[tid]["count"] == 3


def test_shipping_toggle_and_flat_rate_order(monkeypatch, tmp_path):
    import orders
    import merchant_settings
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(merchant_settings, "SETTINGS_FILE", str(tmp_path / "test_settings_shipping.json"))

    tid = "t_shipping_test_123"

    # 1. اختبار تشغيل وإيقاف الشحن حتى مع غياب عمود shipping_enabled في Supabase
    class MockFailingDB:
        def update(self, *a, **k):
            raise Exception("Could not find the column 'shipping_enabled' of 'tenants' in the schema cache")
        def eq(self, *a, **k):
            return self
        def execute(self):
            raise Exception("schema cache error")

    monkeypatch.setattr(orders.supabase, "table", lambda t: MockFailingDB())

    ok, err = orders.set_shipping_enabled(tid, False)
    assert ok is True
    assert err is None
    assert orders.is_shipping_enabled(tid, fresh=True) is False

    ok, err = orders.set_shipping_enabled(tid, True)
    assert ok is True
    assert orders.is_shipping_enabled(tid, fresh=True) is True

    # 2. اختبار endpoint /api/dashboard/{id}/toggle-shipping
    mock_tenant = {
        "id": tid,
        "name": "متجر تجريبي",
        "bot_enabled": True,
        "facebook_page_id": "123",
        "telegram_chat_id": 999,
    }
    monkeypatch.setattr(server, "resolve_tenant_for_dashboard", lambda i: mock_tenant)
    client = TestClient(server.app)

    res = client.post(f"/api/dashboard/{tid}/toggle-shipping")
    assert res.status_code == 200
    json_data = res.json()
    assert json_data["status"] == "success"
    assert "shipping_enabled" in json_data




