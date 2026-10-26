import os
import sys

from dotenv import load_dotenv
from supabase import create_client

sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()
supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SECRET_KEY"],
)


def get_demo_tenant_id():
    result = supabase.table("tenants").select("id,name").limit(1).execute()
    return result.data[0]["id"]


# ===================================================
# ترجمة ثنائية اللغة — عربي ↔ إنجليزي للملابس
# ===================================================
BILINGUAL_MAP = {
    # عربي → إنجليزي
    "تيشيرت":     ["t-shirt", "tshirt", "tee", "t shirt"],
    "هودي":       ["hoodie", "hoody", "hoodies"],
    "سويت شيرت":  ["sweatshirt", "sweater"],
    "كنزة":       ["sweatshirt", "sweater", "knit"],
    "بنطلون":     ["pants", "trousers", "jeans", "jogger"],
    "جينز":       ["jeans", "denim"],
    "شورت":       ["shorts", "short"],
    "كارجو":      ["cargo"],
    "جاكيت":      ["jacket"],
    "بليزر":      ["blazer"],
    "كوت":        ["coat", "overcoat"],
    "بلوفر":      ["pullover", "sweater"],
    "قميص":       ["shirt"],
    "لونج":       ["lounge", "loungewear"],
    "ترنج":       ["tracksuit", "track", "jogger set"],
    "سترة":       ["vest", "jacket"],
    "بيجامة":     ["pajama", "pyjama"],
    "شال":        ["scarf", "shawl"],
    "كاب":        ["cap", "hat"],
    # إنجليزي → عربي
    "t-shirt":    ["تيشيرت"],
    "tshirt":     ["تيشيرت"],
    "tee":        ["تيشيرت"],
    "hoodie":     ["هودي"],
    "sweatshirt": ["سويت شيرت", "كنزة"],
    "jeans":      ["جينز", "بنطلون"],
    "cargo":      ["كارجو"],
    "jacket":     ["جاكيت"],
    "pants":      ["بنطلون"],
    "shorts":     ["شورت"],
    "shirt":      ["قميص"],
    "blazer":     ["بليزر"],
    "pullover":   ["بلوفر"],
    "tracksuit":  ["ترنج"],
    "pajama":     ["بيجامة"],
}

# تطبيع المقاسات (Small → S, Medium → M, Large → L ...)
SIZE_NORMALIZE = {
    "xs": "XS", "extra small": "XS", "x-small": "XS",
    "s": "S", "sm": "S", "small": "S",
    "m": "M", "md": "M", "med": "M", "medium": "M",
    "l": "L", "lg": "L", "large": "L",
    "xl": "XL", "x-large": "XL", "xlarge": "XL", "x large": "XL", "extra large": "XL",
    "xxl": "XXL", "xx-large": "XXL", "xxlarge": "XXL", "2xl": "XXL", "2-xl": "XXL",
    "xxxl": "XXXL", "xxx-large": "XXXL", "3xl": "XXXL", "3-xl": "XXXL",
}


def normalize_size(raw: str) -> str:
    """Small → S, Medium → M, X-Large → XL, إلخ"""
    s = raw.strip().lower()
    return SIZE_NORMALIZE.get(s, raw.strip().upper())


def get_search_aliases(name: str) -> list:
    """بيرجّع قايمة كل الأسماء البديلة (عربي + إنجليزي) للبحث"""
    aliases = [name]
    name_lower = name.strip().lower()

    for key, vals in BILINGUAL_MAP.items():
        key_lower = key.lower()
        # لو اسم المنتج فيه الكلمة أو العكس
        if key_lower in name_lower or name_lower in key_lower:
            aliases.extend(vals)
        # لو اسم المنتج فيه واحدة من البدائل
        for v in vals:
            if v.lower() in name_lower or name_lower in v.lower():
                aliases.append(key)
                aliases.extend(vals)
                break

    # إزالة التكرار مع الحفاظ على الترتيب
    seen = set()
    result = []
    for a in aliases:
        low = a.lower()
        if low not in seen:
            seen.add(low)
            result.append(a)
    return result


def _query_variants(tenant_id, search_term):
    return (
        supabase.table("variants")
        .select("sku,size,color,available,image_url,products!inner(name,price,image_url)")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .ilike("products.name", f"%{search_term}%")
        .execute()
        .data
    )


def check_stock(tenant_id, product_name, size=None, color=None):
    clean_name = product_name.strip()

    # تطبيع المقاس لو موجود
    if size:
        size = normalize_size(size)

    # 1️⃣ بحث مباشر بالاسم كامل
    rows = _query_variants(tenant_id, clean_name)

    # 2️⃣ بحث بكلمة كلمة (لو الاسم مكوّن من كلمتين)
    if not rows and " " in clean_name:
        for word in clean_name.split():
            if len(word) >= 3:
                rows = _query_variants(tenant_id, word)
                if rows:
                    break

    # 3️⃣ بحث ثنائي اللغة — عربي يلاقي إنجليزي والعكس
    if not rows:
        aliases = get_search_aliases(clean_name)
        for alias in aliases[1:]:  # نبدأ من الثاني (الأول هو الاسم الأصلي)
            rows = _query_variants(tenant_id, alias)
            if rows:
                break
            # جرّب بكلمات الـ alias كمان لو مكوّن من كلمتين
            if " " in alias:
                for word in alias.split():
                    if len(word) >= 3:
                        rows = _query_variants(tenant_id, word)
                        if rows:
                            break
            if rows:
                break

    # 4️⃣ لو لسه مفيش — رجّع الكتالوج للـ AI يقارن ذكياً
    if not rows:
        prods = (
            supabase.table("products")
            .select("name,price")
            .eq("tenant_id", tenant_id)
            .eq("active", True)
            .execute()
            .data
        )
        return {
            "status": "not_found",
            "searched": clean_name,
            "catalog": prods or [],
            "message": "الصنف مش موجود بالاسم ده، دي قايمة المنتجات المتاحة في المحل، قارن وشوف لو العميل يقصد واحد منهم أو اقترحهم عليه.",
        }

    # فلترة بالمقاس واللون لو محددين
    wanted = [
        r for r in rows
        if (size is None or r["size"] == size)
        and (color is None or r["color"] == color)
    ]
    in_stock = [r for r in wanted if r["available"] > 0]

    if in_stock:
        return {"status": "available", "items": in_stock}

    alternatives = [r for r in rows if r["available"] > 0]
    return {"status": "out_of_stock", "alternatives": alternatives}


def show(result):
    if result["status"] == "available":
        print("✅ متاح:")
        for r in result["items"]:
            print(f"  - {r['products']['name']} | {r['color']} | {r['size']} "
                  f"| المتاح: {r['available']} | السعر: {r['products']['price']}")
    else:
        print("❌ خلصان. البدائل المتاحة:")
        for r in result.get("alternatives", []):
            print(f"  - {r['products']['name']} | {r['color']} | {r['size']} "
                  f"| المتاح: {r['available']}")


if __name__ == "__main__":
    tenant = get_demo_tenant_id()

    print("--- هودي (يبحث عن Hoodie) ---")
    show(check_stock(tenant, "هودي"))

    print("\n--- Cargo (يبحث عن كارجو) ---")
    show(check_stock(tenant, "Cargo"))

    print("\n--- تيشيرت أسود L ---")
    show(check_stock(tenant, "تيشيرت", size="L", color="أسود"))

    print("\n--- T-Shirt (Large) ---")
    show(check_stock(tenant, "T-Shirt", size="Large"))