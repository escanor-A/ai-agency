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
    rows = (
        supabase.table("variants")
        .select("sku,size,color,available,image_url,products!inner(name,price,image_url)")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .ilike("products.name", f"%{product_name}%")
        .execute()
        .data
    )

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