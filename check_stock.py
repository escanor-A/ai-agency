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


def check_stock(tenant_id, product_name, size=None, color=None):
    clean_name = product_name.strip()
    rows = (
        supabase.table("variants")
        .select("sku,size,color,available,image_url,products!inner(name,price,image_url)")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .ilike("products.name", f"%{clean_name}%")
        .execute()
        .data
    )

    # لو الاسم مش متطابق بالظبط (مثلاً فيه غلطة إملائية أو كلمة زيادة)، نجرب بأهم كلمات
    if not rows and " " in clean_name:
        for word in clean_name.split():
            if len(word) >= 3:
                r_word = (
                    supabase.table("variants")
                    .select("sku,size,color,available,image_url,products!inner(name,price,image_url)")
                    .eq("tenant_id", tenant_id)
                    .eq("active", True)
                    .ilike("products.name", f"%{word}%")
                    .execute()
                    .data
                )
                if r_word:
                    rows = r_word
                    break

    # لو لسه مفيش نتايج، نرجع كتالوج المحل المتاح للـ AI علشان يقارن ويفهم ذكياً
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
            "message": "الصنف مش مطابق حرفياً، دي قايمة المنتجات المتاحة في المحل، قارن وشوف لو العميل يقصد واحد منهم أو اقترحهم عليه."
        }

    wanted = [
        r
        for r in rows
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
        for r in result["alternatives"]:
            print(f"  - {r['products']['name']} | {r['color']} | {r['size']} "
                  f"| المتاح: {r['available']}")


if __name__ == "__main__":
    tenant = get_demo_tenant_id()

    print("--- تيشيرت أسود L ---")
    show(check_stock(tenant, "تيشيرت", size="L", color="أسود"))

    print("\n--- تيشيرت أسود XL (المفروض خلصان) ---")
    show(check_stock(tenant, "تيشيرت", size="XL", color="أسود"))