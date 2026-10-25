import os
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

import orders
from check_stock import check_stock as db_check_stock
from check_stock import get_demo_tenant_id

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# لو ظهر خطأ إن الموديل مش موجود، خد اسم موديل Flash متاح من Google AI Studio
MODEL = "gemini-3.5-flash-lite"

TENANT_ID = get_demo_tenant_id()

SYSTEM_PROMPT = """أنت بياع شاطر في براند ملابس مصري، بترد على العملاء على السوشيال ميديا.

أسلوبك:
- بتتكلم باللهجة المصرية البسيطة والودودة.
- ردودك قصيرة (جملتين أو تلاتة) زي واحد بيكلم على واتساب.
- بتستخدم إيموجي بسيط وبحساب.

قواعد المخزن:
1. ممنوع تقول إن حاجة متاحة أو خلصانة من غير ما تنادي على أداة check_stock الأول.
2. ممنوع تخترع سعر أو منتج أو لون أو مقاس مش راجع من الأدوات.
3. لو الصنف خلصان، اقترح فوراً بدائل من اللي الأداة رجّعته بس.
4. لو العميل ماحددش مقاس أو لون، اسأله بدل ما تخمّن.
5. لو العميل سأل عن حاجة مالهاش علاقة بالمنتجات، رد بلطف وارجّعه للموضوع.

خطوات تسجيل الأوردر (الدفع عند الاستلام فقط):
1. لما العميل يقرر يشتري، اجمع اللي ناقص بس: الاسم، رقم الموبايل، المحافظة، العنوان بالتفصيل.
2. نادِ quote_order علشان تجيب الإجمالي. ممنوع تحسب الأسعار أو الشحن بنفسك.
3. اعرض ملخص واضح: الأصناف والكميات، سعر القطع، الشحن، الإجمالي، وإن الدفع عند الاستلام.
4. استنى العميل يأكد صراحة (زي: تمام، أكد، ماشي). ممنوع تسجّل الأوردر قبل التأكيد.
5. بعد التأكيد نادِ place_order مرة واحدة بس. ممنوع تناديها مرتين لنفس الأوردر.
6. لو نجح، قول رقم الأوردر والإجمالي وإنهم هيتصلوا بيه للتوصيل.
7. لو فشل، اشرح السبب ببساطة. لو الكمية خلصت اعتذر واقترح بديل من check_stock.
8. لو العميل غيّر رأيه أو عدّل حاجة، ارجع لخطوة 2 واعرض ملخص جديد.

معلومات عن المخزن:
- المقاسات المسجلة: M و L و XL (لو العميل قال "لارج" يبقى L، و"ميديم" يبقى M، و"اكس لارج" يبقى XL).
- الألوان المسجلة بالعربي: أسود، أبيض، رمادي، كحلي.
- الأقسام: تيشيرت، هودي، بنطلون.
- الشحن متاح لكل محافظات مصر وبيتحدد سعره حسب المحافظة.
"""


def check_stock(product_name: str, size: str = "", color: str = "") -> dict:
    """بتكشف في المخزن عن منتج بمقاس ولون معين.
    بترجّع الكميات المتاحة والسعر وكود الصنف (sku)، ولو الصنف خلصان بترجّع بدائل متاحة.

    Args:
        product_name: اسم المنتج أو جزء منه، مثلاً: تيشيرت أو هودي أو بنطلون
        size: المقاس بالحروف الإنجليزية: M أو L أو XL (سيبه فاضي لو العميل ماحددش)
        color: اللون بالعربي: أسود أو أبيض أو رمادي أو كحلي (سيبه فاضي لو العميل ماحددش)
    """
    print(f"   [🔧 كشف المخزن: {product_name} / {size} / {color}]")
    return db_check_stock(
        TENANT_ID, product_name, size=size or None, color=color or None
    )


def quote_order(items: list[str], governorate: str) -> dict:
    """بتحسب سعر الأوردر والشحن والإجمالي من غير ما تسجّل حاجة.
    لازم تناديها قبل ما تعرض الملخص على العميل.

    Args:
        items: قايمة الأصناف، كل عنصر نص بالشكل 'كود_الصنف:الكمية' مثلاً ['TS-BLK-L:2']. الكود (sku) بييجي من check_stock.
        governorate: اسم المحافظة بالعربي، مثلاً: القاهرة
    """
    print(f"   [🧮 حساب الأوردر: {items} / {governorate}]")
    return orders.quote_order(TENANT_ID, items, governorate)


def place_order(
    customer_name: str,
    phone: str,
    governorate: str,
    address: str,
    items: list[str],
) -> dict:
    """بتسجّل الأوردر فعلياً وبتخصم من المخزن. ماتناديهاش غير بعد ما العميل يأكد الملخص صراحة.

    Args:
        customer_name: اسم العميل
        phone: رقم موبايل العميل (11 رقم مصري)
        governorate: اسم المحافظة بالعربي
        address: العنوان بالتفصيل (الشارع، المنطقة، علامة مميزة)
        items: قايمة الأصناف بالشكل 'كود_الصنف:الكمية' مثلاً ['TS-BLK-L:2']
    """
    print(f"   [📦 تسجيل أوردر: {customer_name} / {governorate} / {items}]")
    return orders.place_order(
        TENANT_ID, customer_name, phone, governorate, address, items
    )


config = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[check_stock, quote_order, place_order],
)


def send_with_retry(chat, text):
    for attempt in range(4):
        try:
            return chat.send_message(text).text
        except Exception as e:
            busy = "503" in str(e) or "429" in str(e)
            if busy and attempt < 3:
                wait = 5 * (attempt + 1)
                print(f"   [⏳ الموديل مشغول، هحاول تاني بعد {wait} ثانية...]")
                time.sleep(wait)
            else:
                return f"⚠️ حصل خطأ: {e}"


if __name__ == "__main__":
    print("اكتب رسالة كأنك عميل. اكتب 'خروج' للإنهاء.\n")
    chat = client.chats.create(model=MODEL, config=config)
    while True:
        text = input("العميل: ").strip()
        if text in ("خروج", "exit", "quit"):
            break
        if not text:
            continue
        print("البوت:", send_with_retry(chat, text), "\n")