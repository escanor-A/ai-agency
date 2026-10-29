import re
import threading
import time
import uuid
import notify

from check_stock import supabase

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# تتبع عروض الأسعار الناجحة (Quote Verification) لكل جلسة خلال 15 دقيقة
# {session_id: [{"quote_id": str, "items": tuple, "governorate": str, "applied_code": str, "total": float, "timestamp": float}]}
SESSION_QUOTES = {}
SESSION_QUOTES_LOCK = threading.Lock()
QUOTE_TTL_SECONDS = 15 * 60  # 15 دقيقة


def cleanup_expired_session_quotes(now: float = None):
    """تنظيف دوري للجلسات وعروض الأسعار المنتهية الصلاحية من الذاكرة (يجب استدعاؤها تحت SESSION_QUOTES_LOCK)"""
    if now is None:
        now = time.time()
    expired_sessions = []
    for sid, quotes in list(SESSION_QUOTES.items()):
        valid = [q for q in quotes if now - q.get("timestamp", 0) <= QUOTE_TTL_SECONDS]
        if valid:
            SESSION_QUOTES[sid] = valid
        else:
            expired_sessions.append(sid)
    for sid in expired_sessions:
        SESSION_QUOTES.pop(sid, None)


def normalize_items_key(items) -> tuple:
    """توحيد الأصناف لمقارنتها بدقة بغض النظر عن الترتيب أو المسافات أو حالة الأحرف"""
    if not items:
        return ()
    counts = {}
    for raw in items:
        sku, _, qty_text = str(raw).partition(":")
        sku = sku.strip().upper()
        try:
            qty = int(qty_text.strip() or 1)
        except (ValueError, TypeError):
            qty = 1
        if sku and qty > 0:
            counts[sku] = counts.get(sku, 0) + qty
    return tuple(sorted(counts.items()))


def record_successful_quote(session_id: str, items: list, governorate: str, applied_code: str = None, total: float = 0.0, quote_id: str = None) -> str:
    """تسجيل حساب أوردر ناجح في الجلسة مع حفظ quote_id و applied_code و total لاستخدامه في التحقق عند place_order"""
    qid = quote_id or str(uuid.uuid4())
    if not session_id:
        return qid
    now = time.time()
    norm_items = normalize_items_key(items)
    norm_gov = normalize_text(governorate).lower()

    with SESSION_QUOTES_LOCK:
        cleanup_expired_session_quotes(now)
        quotes = SESSION_QUOTES.setdefault(str(session_id), [])
        quotes.append({
            "quote_id": qid,
            "items": norm_items,
            "governorate": norm_gov,
            "applied_code": applied_code,
            "total": round(float(total), 2),
            "timestamp": now,
        })
    return qid


def get_valid_quote(session_id: str, items: list, governorate: str) -> dict | None:
    """استرجاع آخر quote_order صالح ومطابق في نفس الجلسة بنفس الأصناف والمحافظة خلال 15 دقيقة"""
    if not session_id:
        return None
    now = time.time()
    norm_items = normalize_items_key(items)
    norm_gov = normalize_text(governorate).lower()

    with SESSION_QUOTES_LOCK:
        cleanup_expired_session_quotes(now)
        quotes = SESSION_QUOTES.get(str(session_id), [])
        for q in reversed(quotes):
            if now - q.get("timestamp", 0) <= QUOTE_TTL_SECONDS:
                if q["items"] == norm_items and q["governorate"] == norm_gov:
                    return q
    return None


def has_valid_quote(session_id: str, items: list, governorate: str) -> bool:
    """التحقق من وجود quote_order ناجح في نفس الجلسة بنفس الأصناف والمحافظة خلال 15 دقيقة"""
    return get_valid_quote(session_id, items, governorate) is not None


def normalize_text(text):
    """بيوحّد الهمزات والتاء المربوطة علشان 'القاهره' = 'القاهرة'"""
    text = text.strip()
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        text = text.replace(a, b)
    return text


def normalize_phone(raw):
    """بيرجّع رقم موبايل مصري صحيح (11 رقم) أو None"""
    digits = re.sub(r"\D", "", raw.translate(ARABIC_DIGITS))
    if digits.startswith("20") and len(digits) == 12:
        digits = "0" + digits[2:]
    if re.fullmatch(r"01[0125]\d{8}", digits):
        return digits
    return None


def find_shipping(tenant_id, governorate):
    rows = (
        supabase.table("shipping_rates")
        .select("governorate,price")
        .eq("tenant_id", tenant_id)
        .execute()
        .data
    )
    wanted = normalize_text(governorate)
    for r in rows:
        if normalize_text(r["governorate"]) == wanted:
            return r
    return None


def load_items(tenant_id, items):
    """
    items: قايمة نصوص بالشكل 'SKU:الكمية' مثلاً ['TS-BLK-L:2']
    بيرجّع (lines, error). كل سطر فيه المنتج والسعر والكمية.
    """
    wanted = {}
    for raw in items:
        sku, _, qty_text = raw.partition(":")
        sku = sku.strip().upper()
        try:
            qty = int(qty_text.strip() or 1)
        except ValueError:
            return None, f"الكمية غلط في: {raw}"
        if not sku or qty <= 0:
            return None, f"بيانات غلط في: {raw}"
        wanted[sku] = wanted.get(sku, 0) + qty

    rows = (
        supabase.table("variants")
        .select("id,sku,size,color,available,products!inner(name,price)")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .in_("sku", list(wanted))
        .execute()
        .data
    )
    by_sku = {r["sku"]: r for r in rows}

    lines = []
    for sku, qty in wanted.items():
        r = by_sku.get(sku)
        if not r:
            return None, f"الكود {sku} مش موجود"
        if r["available"] < qty:
            return None, f"{r['products']['name']} {r['color']} {r['size']}: الكمية المطلوبة غير متوفرة حالياً"
        price = float(r["products"]["price"])
        lines.append(
            {
                "variant_id": r["id"],
                "sku": sku,
                "name": r["products"]["name"],
                "color": r["color"],
                "size": r["size"],
                "qty": qty,
                "unit_price": price,
                "line_total": price * qty,
            }
        )
    return lines, None


def get_active_promotions(tenant_id):
    """بترجع كل العروض النشطة الخاصة بالتاجر"""
    try:
        rows = (
            supabase.table("promotions")
            .select("code,promo_type,discount_value,min_order_items,description")
            .eq("tenant_id", tenant_id)
            .eq("is_active", True)
            .execute()
            .data
        )
        return rows or []
    except Exception:
        return []


def quote_order(tenant_id, items, governorate, promo_code="", session_id=None):
    """حساب السعر والشحن مع تطبيق العروض التلقائية أو أكواد الخصم"""
    ship = find_shipping(tenant_id, governorate)
    if not ship:
        return {"status": "error", "reason": "unknown_governorate"}
    lines, error = load_items(tenant_id, items)
    if error:
        return {"status": "error", "reason": error}
    subtotal = sum(l["line_total"] for l in lines)
    fee = float(ship["price"])
    total_qty = sum(l["qty"] for l in lines)

    discount = 0.0
    discount_note = ""
    applied_code = None
    promo_error = ""

    # فحص العروض المسجلة للتاجر
    promos = get_active_promotions(tenant_id)
    clean_code = (promo_code or "").strip().upper()

    # 1. تطبيق العروض التلقائية (شحن مجاني على قطعتين مثلاً)
    for p in promos:
        if not p.get("code") and p.get("promo_type") == "free_shipping":
            if total_qty >= int(p.get("min_order_items") or 1):
                fee = 0.0
                discount_note = p.get("description", "عرض شحن مجاني")
                break

    # 2. تطبيق كود الخصم لو العميل دخله
    if clean_code:
        matched = next((p for p in promos if (p.get("code") or "").upper() == clean_code), None)
        if matched:
            applied_code = clean_code
            ptype = matched.get("promo_type")
            val = float(matched.get("discount_value") or 0)
            if ptype == "percentage":
                discount = subtotal * (val / 100.0)
                discount_note = f"كود خصم {clean_code} (-{val:.0f}%)"
            elif ptype == "fixed":
                discount = min(subtotal, val)
                discount_note = f"كود خصم {clean_code} (-{val:.0f} جنيه)"
            elif ptype == "free_shipping":
                fee = 0.0
                discount_note = f"كود خصم {clean_code} (شحن مجاني)"
        else:
            promo_error = f"كود الخصم '{promo_code}' غير صالح أو منتهي"

    total = max(0.0, subtotal - discount) + fee
    quote_id = str(uuid.uuid4())

    res = {
        "status": "ok",
        "quote_id": quote_id,
        "governorate": ship["governorate"],
        "lines": [
            {k: l[k] for k in ("name", "color", "size", "qty", "unit_price", "line_total")}
            for l in lines
        ],
        "subtotal": subtotal,
        "discount": discount,
        "discount_note": discount_note,
        "applied_code": applied_code,
        "promo_error": promo_error,
        "shipping_fee": fee,
        "total": total,
        "payment": "الدفع عند الاستلام",
    }
    if session_id:
        record_successful_quote(
            session_id, items, governorate,
            applied_code=applied_code, total=total, quote_id=quote_id
        )
    return res


def normalize_channel(ch):
    """بيوحّد اسم القناة ليتوافق مع قيود قاعدة البيانات (whatsapp, instagram, messenger, tiktok)"""
    ch = (ch or "").lower().strip()
    if "tiktok" in ch:
        return "tiktok"
    if ch.startswith("wa") or "whats" in ch:
        return "whatsapp"
    if ch.startswith("ig") or "insta" in ch:
        return "instagram"
    return "messenger"


def get_or_create_customer(tenant_id, channel, channel_user_id, name="", phone=""):
    """
    تسجيل أو جلب هوية العميل في جدول customers
    channel: 'whatsapp' | 'instagram' | 'messenger' | 'tiktok'
    channel_user_id: معرّف العميل على المنصة
    """
    if not tenant_id or not channel_user_id:
        return None

    clean_channel = normalize_channel(channel)
    user_str = str(channel_user_id).strip()
    clean_phone = normalize_phone(phone) if phone else None

    # البحث عن العميل
    try:
        rows = (
            supabase.table("customers")
            .select("id,name,phone")
            .eq("tenant_id", tenant_id)
            .eq("channel", clean_channel)
            .eq("channel_user_id", user_str)
            .execute()
            .data
        )
        if rows:
            cust = rows[0]
            updates = {}
            if name and not cust.get("name"):
                updates["name"] = name.strip()
            if clean_phone and not cust.get("phone"):
                updates["phone"] = clean_phone
            if updates:
                supabase.table("customers").update(updates).eq("id", cust["id"]).execute()
            return cust["id"]

        # إنشاء العميل
        new_row = {
            "tenant_id": tenant_id,
            "channel": clean_channel,
            "channel_user_id": user_str,
            "name": name.strip() if name else None,
            "phone": clean_phone,
        }
        res = supabase.table("customers").insert(new_row).execute()
        if res.data:
            return res.data[0]["id"]
    except Exception as e:
        print("خطأ في تسجيل أو جلب العميل:", e)
        try:
            rows = (
                supabase.table("customers")
                .select("id")
                .eq("tenant_id", tenant_id)
                .eq("channel", clean_channel)
                .eq("channel_user_id", user_str)
                .execute()
                .data
            )
            if rows:
                return rows[0]["id"]
        except Exception:
            pass
    return None


def place_order(
    tenant_id, customer_name, phone, governorate, address, items,
    channel="messenger", channel_user_id=None, promo_code="", session_id=None
):
    """تسجيل الأوردر فعلياً وخصم المخزون وربطه بهوية العميل وتطبيق العروض وأكواد الخصم بمفتاح idempotency"""
    if len(customer_name.strip()) < 2:
        return {"status": "error", "reason": "الاسم ناقص"}
    clean_phone = normalize_phone(phone)
    if not clean_phone:
        return {"status": "error", "reason": "رقم الموبايل غلط (لازم 11 رقم مصري)"}
    if len(address.strip()) < 10:
        return {"status": "error", "reason": "العنوان ناقص، محتاج تفاصيل أكتر"}

    # 1. التأكد من وجود quote_order ناجح في نفس الجلسة بنفس الأصناف والمحافظة خلال 15 دقيقة
    effective_session_id = session_id or (f"{tenant_id}:{channel}:{channel_user_id}" if channel_user_id else None)
    if not effective_session_id:
        return {
            "status": "error",
            "reason": "يجب حساب تكلفة الأوردر بـ quote_order أولاً في نفس الجلسة بنفس الأصناف والمحافظة قبل تأكيد الطلب"
        }

    valid_quote = get_valid_quote(effective_session_id, items, governorate)
    if not valid_quote:
        return {
            "status": "error",
            "reason": "يجب حساب تكلفة الأوردر بـ quote_order أولاً في نفس الجلسة بنفس الأصناف والمحافظة قبل تأكيد الطلب"
        }

    quote_id = valid_quote["quote_id"]
    stored_code = valid_quote.get("applied_code") or ""
    stored_total = valid_quote.get("total", 0.0)

    # 2. إعادة حساب الفاتورة بالكود المخزن (تجاهل أي كود مرره الموديل) والتحقق من تطابق الإجمالي
    quote = quote_order(tenant_id, items, governorate, promo_code=stored_code, session_id=None)
    if quote.get("status") != "ok":
        return quote

    if abs(float(quote["total"]) - float(stored_total)) > 0.01:
        return {
            "status": "error",
            "reason": "order_total_mismatch",
            "message": f"تغير إجمالي الأوردر المحسوب ({quote['total']}) عن المسعر سابقاً ({stored_total})، يرجى إعادة حساب الأوردر للعميل"
        }

    ship = find_shipping(tenant_id, governorate)
    if not ship:
        return {"status": "error", "reason": "unknown_governorate"}

    lines, error = load_items(tenant_id, items)
    if error:
        return {"status": "error", "reason": error}

    # تسجيل هوية العميل أولاً
    cust_uuid = None
    if channel_user_id:
        cust_uuid = get_or_create_customer(
            tenant_id, channel, channel_user_id, name=customer_name.strip(), phone=clean_phone
        )

    try:
        result = supabase.rpc(
            "place_order_v3",
            {
                "p_tenant": tenant_id,
                "p_customer_name": customer_name.strip(),
                "p_phone": clean_phone,
                "p_governorate": ship["governorate"],
                "p_address": address.strip(),
                "p_items": [{"variant_id": l["variant_id"], "qty": l["qty"]} for l in lines],
                "p_customer": cust_uuid,
                "p_shipping_fee": quote["shipping_fee"],
                "p_discount": quote.get("discount", 0.0),
                "p_discount_note": quote.get("discount_note") or None,
                "p_promo_code": quote.get("applied_code"),
                "p_idempotency_key": quote_id,
            },
        ).execute()
    except Exception as e:
        if "INSUFFICIENT_STOCK" in str(e):
            return {"status": "error", "reason": "الكمية خلصت في اللحظة الأخيرة"}
        print("خطأ في تسجيل الأوردر:", e)
        return {"status": "error", "reason": "system_error"}

    order = {"status": "ok", **result.data}
    if quote.get("promo_error"):
        order["promo_error"] = quote["promo_error"]

    # حماية التكرار (Idempotency): لو الأوردر مكرر بنفس مفتاح quote_id نرجعه دون إشعار تاجر
    if result.data and result.data.get("duplicate") is True:
        print(f"🔁 طلب مكرر (idempotent key: {quote_id}) — إرجاع نفس الأوردر دون إرسال إشعار تاجر جديد")
        return order

    try:
        notify.send_order_notification(
            tenant_id, order, customer_name.strip(), clean_phone,
            ship["governorate"], address.strip(), lines,
        )
    except Exception:
        pass  # فشل الإشعار ما يبوّظش الأوردر
    return order


def cancel_order(tenant_id, order_number, reason=""):
    """إلغاء أوردر ورجوع كمياته للمخزن لو كان ساري، وإشعار التاجر فوراً"""
    try:
        order_num = int(order_number)
    except (ValueError, TypeError):
        return {"status": "error", "reason": "رقم الأوردر مش صحيح"}

    # 1. هات الأوردر
    rows = (
        supabase.table("orders")
        .select("id,status,customer_name")
        .eq("tenant_id", tenant_id)
        .eq("order_number", order_num)
        .execute()
        .data
    )
    if not rows:
        return {"status": "error", "reason": f"الأوردر #{order_num} مش موجود"}

    order = rows[0]
    if order["status"] not in ("new", "confirmed"):
        if order["status"] == "cancelled":
            return {"status": "error", "reason": f"الأوردر #{order_num} ملغي بالفعل"}
        return {"status": "error", "reason": f"لا يمكن إلغاء أوردر حالته {order['status']} (مسموح بالإلغاء فقط في حالة new و confirmed)"}

    # 2. هات عناصر الأوردر عشان نرجعها للمخزن
    items = (
        supabase.table("order_items")
        .select("variant_id,qty")
        .eq("order_id", order["id"])
        .execute()
        .data
    )

    # 3. رجّع الكميات لحركة المخزون مع إضافة variant_id للمرجع لمنع تجاوز الأصناف التالية
    for item in items:
        try:
            supabase.rpc(
                "apply_stock_movement",
                {
                    "p_tenant": tenant_id,
                    "p_variant": item["variant_id"],
                    "p_change": item["qty"],
                    "p_reason": "return",
                    "p_source": "order_cancelled",
                    "p_ref": f"cancel-order-{order_num}-{item['variant_id']}",
                },
            ).execute()
        except Exception as e:
            print("خطأ في إعادة كمية الصنف:", e)

    # 4. حدّث حالة الأوردر لـ cancelled
    supabase.table("orders").update({"status": "cancelled"}).eq("id", order["id"]).execute()

    # 5. إشعار التاجر على تليجرام
    try:
        notify.send_order_cancellation_notification(
            tenant_id, order_num, order.get("customer_name", ""), reason
        )
    except Exception:
        pass

    return {"status": "ok", "order_number": order_num}


def update_order_shipping(tenant_id, order_number, phone="", address=""):
    """تعديل رقم الموبايل أو عنوان الشحن لأوردر قائم"""
    try:
        order_num = int(order_number)
    except (ValueError, TypeError):
        return {"status": "error", "reason": "رقم الأوردر مش صحيح"}

    rows = (
        supabase.table("orders")
        .select("id,status,phone,address,customer_name")
        .eq("tenant_id", tenant_id)
        .eq("order_number", order_num)
        .execute()
        .data
    )
    if not rows:
        return {"status": "error", "reason": f"الأوردر #{order_num} مش موجود"}

    order = rows[0]
    if order["status"] not in ("new", "confirmed"):
        return {"status": "error", "reason": f"مستحيل تعديل أوردر حالته {order['status']} (مسموح بالتعديل فقط في حالة new و confirmed)"}

    update_data = {}
    changes = []
    if phone:
        clean_phone = normalize_phone(phone)
        if not clean_phone:
            return {"status": "error", "reason": "رقم الموبايل غلط"}
        update_data["phone"] = clean_phone
        changes.append(f"• الموبايل الجديد: {clean_phone}")

    if address and len(address.strip()) >= 5:
        update_data["address"] = address.strip()
        changes.append(f"• العنوان الجديد: {address.strip()}")

    if not update_data:
        return {"status": "error", "reason": "مفيش بيانات جديدة للتعديل"}

    supabase.table("orders").update(update_data).eq("id", order["id"]).execute()

    # إشعار التاجر
    try:
        notify.send_order_update_notification(tenant_id, order_num, "\n".join(changes))
    except Exception:
        pass

    return {"status": "ok", "order_number": order_num, "changes": changes}


def update_order_status(tenant_id, order_number, new_status):
    """تحديث حالة الأوردر (shipped / delivered / confirmed)"""
    try:
        order_num = int(order_number)
    except (ValueError, TypeError):
        return {"status": "error", "reason": "رقم الأوردر مش صحيح"}

    valid_statuses = {"new", "confirmed", "shipped", "delivered", "cancelled"}
    if new_status not in valid_statuses:
        return {"status": "error", "reason": f"الحالة غلط. المتاح: {', '.join(valid_statuses)}"}

    if new_status == "cancelled":
        return cancel_order(tenant_id, order_num)

    res = supabase.table("orders").update({"status": new_status}).eq("tenant_id", tenant_id).eq("order_number", order_num).execute()
    if not res.data:
        return {"status": "error", "reason": f"الأوردر #{order_num} مش موجود"}

    return {"status": "ok", "order_number": order_num, "new_status": new_status}