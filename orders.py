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


def quote_order(tenant_id, items, governorate, promo_code=""):
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
            discount_note = f"كود الخصم '{promo_code}' غير صالح أو منتهي"

    total = max(0.0, subtotal - discount) + fee

    return {
        "status": "ok",
        "governorate": ship["governorate"],
        "lines": [
            {k: l[k] for k in ("name", "color", "size", "qty", "unit_price", "line_total")}
            for l in lines
        ],
        "subtotal": subtotal,
        "discount": discount,
        "discount_note": discount_note,
        "shipping_fee": fee,
        "total": total,
        "payment": "الدفع عند الاستلام",
    }


def normalize_channel(ch):
    """بيوحّد اسم القناة ليتوافق مع قيود قاعدة البيانات (whatsapp, instagram, messenger)"""
    ch = (ch or "").lower().strip()
    if "wa" in ch or "whats" in ch:
        return "whatsapp"
    if "ig" in ch or "insta" in ch:
        return "instagram"
    return "messenger"


def get_or_create_customer(tenant_id, channel, channel_user_id, name="", phone=""):
    """
    تسجيل أو جلب هوية العميل في جدول customers
    channel: 'whatsapp' | 'instagram' | 'messenger'
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


def place_order(tenant_id, customer_name, phone, governorate, address, items, channel="messenger", channel_user_id=None, promo_code=""):
    """تسجيل الأوردر فعلياً وخصم المخزون وربطه بهوية العميل وتطبيق العروض وأكواد الخصم"""
    if len(customer_name.strip()) < 2:
        return {"status": "error", "reason": "الاسم ناقص"}
    clean_phone = normalize_phone(phone)
    if not clean_phone:
        return {"status": "error", "reason": "رقم الموبايل غلط (لازم 11 رقم مصري)"}
    if len(address.strip()) < 10:
        return {"status": "error", "reason": "العنوان ناقص، محتاج تفاصيل أكتر"}

    # 1. حساب الفاتورة الحقيقية والعروض والشحن وكود الخصم أولاً
    quote = quote_order(tenant_id, items, governorate, promo_code=promo_code)
    if quote.get("status") != "ok":
        return quote

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

    # تحديث مصاريف الشحن والإجمالي والعميل في جدول orders
    # لأن دالة الداتابيز place_order لا تأخذ العروض وأكواد الخصم في الحسبان
    order_updates = {
        "shipping_fee": quote["shipping_fee"],
        "total": quote["total"],
    }
    if cust_uuid:
        order_updates["customer_id"] = cust_uuid

    order_id = order.get("id")
    order_num = order.get("order_number")
    if order_id or order_num:
        try:
            q = supabase.table("orders").update(order_updates).eq("tenant_id", tenant_id)
            if order_id:
                q = q.eq("id", order_id)
            else:
                q = q.eq("order_number", order_num)
            q.execute()
        except Exception as e:
            print("خطأ في تحديث بيانات الأوردر وعروض الخصم:", e)

    # تحديث كائن order المرجع ليعكس الخصم والشحن الفعلي
    order["shipping_fee"] = quote["shipping_fee"]
    order["total"] = quote["total"]
    order["discount"] = quote.get("discount", 0.0)
    order["discount_note"] = quote.get("discount_note", "")

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