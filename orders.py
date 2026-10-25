import re
import notify

from check_stock import supabase

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


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
        sku = sku.strip()
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
            return None, f"{r['products']['name']} {r['color']} {r['size']}: المتاح {r['available']} بس"
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


def quote_order(tenant_id, items, governorate):
    """حساب السعر والشحن من غير ما يسجّل أي حاجة"""
    ship = find_shipping(tenant_id, governorate)
    if not ship:
        return {"status": "error", "reason": "unknown_governorate"}
    lines, error = load_items(tenant_id, items)
    if error:
        return {"status": "error", "reason": error}
    subtotal = sum(l["line_total"] for l in lines)
    fee = float(ship["price"])
    return {
        "status": "ok",
        "governorate": ship["governorate"],
        "lines": [
            {k: l[k] for k in ("name", "color", "size", "qty", "unit_price", "line_total")}
            for l in lines
        ],
        "subtotal": subtotal,
        "shipping_fee": fee,
        "total": subtotal + fee,
        "payment": "الدفع عند الاستلام",
    }


def place_order(tenant_id, customer_name, phone, governorate, address, items):
    """تسجيل الأوردر فعلياً وخصم المخزون (في خطوة واحدة داخل الداتابيز)"""
    if len(customer_name.strip()) < 2:
        return {"status": "error", "reason": "الاسم ناقص"}
    clean_phone = normalize_phone(phone)
    if not clean_phone:
        return {"status": "error", "reason": "رقم الموبايل غلط (لازم 11 رقم مصري)"}
    if len(address.strip()) < 10:
        return {"status": "error", "reason": "العنوان ناقص، محتاج تفاصيل أكتر"}

    ship = find_shipping(tenant_id, governorate)
    if not ship:
        return {"status": "error", "reason": "unknown_governorate"}

    lines, error = load_items(tenant_id, items)
    if error:
        return {"status": "error", "reason": error}

    try:
        result = supabase.rpc(
            "place_order",
            {
                "p_tenant": tenant_id,
                "p_customer_name": customer_name.strip(),
                "p_phone": clean_phone,
                "p_governorate": ship["governorate"],
                "p_address": address.strip(),
                "p_items": [{"variant_id": l["variant_id"], "qty": l["qty"]} for l in lines],
            },
        ).execute()
    except Exception as e:
        if "INSUFFICIENT_STOCK" in str(e):
            return {"status": "error", "reason": "الكمية خلصت في اللحظة الأخيرة"}
        print("خطأ في تسجيل الأوردر:", e)
        return {"status": "error", "reason": "system_error"}

    order = {"status": "ok", **result.data}
    try:
        notify.send_order_notification(
            tenant_id, order, customer_name.strip(), clean_phone,
            ship["governorate"], address.strip(), lines,
        )
    except Exception:
        pass  # فشل الإشعار ما يبوّظش الأوردر
    return order


def cancel_order(tenant_id, order_number):
    """إلغاء أوردر ورجوع كمياته للمخزن لو كان ساري"""
    try:
        order_num = int(order_number)
    except (ValueError, TypeError):
        return {"status": "error", "reason": "رقم الأوردر مش صحيح"}

    # 1. هات الأوردر
    rows = (
        supabase.table("orders")
        .select("id,status")
        .eq("tenant_id", tenant_id)
        .eq("order_number", order_num)
        .execute()
        .data
    )
    if not rows:
        return {"status": "error", "reason": f"الأوردر #{order_num} مش موجود"}

    order = rows[0]
    if order["status"] == "cancelled":
        return {"status": "error", "reason": f"الأوردر #{order_num} ملغي بالفعل"}

    # 2. هات عناصر الأوردر عشان نرجعها للمخزن
    items = (
        supabase.table("order_items")
        .select("variant_id,qty")
        .eq("order_id", order["id"])
        .execute()
        .data
    )

    # 3. رجّع الكميات لحركة المخزون
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
                    "p_ref": f"cancel-order-{order_num}",
                },
            ).execute()
        except Exception as e:
            print("خطأ في إعادة كمية الصنف:", e)

    # 4. حدّث حالة الأوردر لـ cancelled
    supabase.table("orders").update({"status": "cancelled"}).eq("id", order["id"]).execute()
    return {"status": "ok", "order_number": order_num}