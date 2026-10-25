import os
import sys

from dotenv import load_dotenv
from google import genai
from google.genai import types

from check_stock import check_stock as db_check_stock
from check_stock import get_demo_tenant_id

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# لو ظهر خطأ إن الموديل مش موجود، افتح Google AI Studio وخد اسم موديل Flash متاح
MODEL = "gemini-3.5-flash-lite"

TENANT_ID = get_demo_tenant_id()

SYSTEM_PROMPT = """أنت بياع شاطر في براند ملابس مصري، بترد على العملاء على السوشيال ميديا.

أسلوبك:
- بتتكلم باللهجة المصرية البسيطة والودودة.
- ردودك قصيرة (جملتين أو تلاتة) زي واحد بيكلم على واتساب.
- بتستخدم إيموجي بسيط وبحساب.

قواعد صارمة:
1. ممنوع تقول إن حاجة متاحة أو خلصانة من غير ما تنادي على أداة check_stock الأول.
2. ممنوع تخترع سعر أو منتج أو لون أو مقاس مش راجع من الأداة.
3. لو الصنف خلصان، اقترح فوراً بدائل من اللي الأداة رجّعته بس.
4. لو العميل ماحددش مقاس أو لون، اسأله بدل ما تخمّن.
5. لو العميل سأل عن حاجة مالهاش علاقة بالمنتجات، رد بلطف وارجّعه للموضوع.
6. تسجيل الأوردرات لسه مش متفعّل. لو العميل قرر يشتري، قوله إنك هتسجل له الأوردر قريب.

معلومات عن المخزن:
- المقاسات المسجلة: M و L و XL (لو العميل قال "لارج" يبقى L، و"ميديم" يبقى M، و"اكس لارج" يبقى XL).
- الألوان المسجلة بالعربي: أسود، أبيض، رمادي، كحلي.
- الأقسام: تيشيرت، هودي، بنطلون.
"""


def check_stock(product_name: str, size: str = "", color: str = "") -> dict:
    """بتكشف في المخزن عن منتج بمقاس ولون معين.
    بترجّع الكميات المتاحة والسعر، ولو الصنف خلصان بترجّع بدائل متاحة.

    Args:
        product_name: اسم المنتج أو جزء منه، مثلاً: تيشيرت أو هودي أو بنطلون
        size: المقاس بالحروف الإنجليزية: M أو L أو XL (سيبه فاضي لو العميل ماحددش)
        color: اللون بالعربي: أسود أو أبيض أو رمادي أو كحلي (سيبه فاضي لو العميل ماحددش)
    """
    print(f"   [🔧 البوت بيكشف في المخزن: {product_name} / {size} / {color}]")
    return db_check_stock(
        TENANT_ID,
        product_name,
        size=size or None,
        color=color or None,
    )


config = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[check_stock],  # المكتبة بتنادي الدالة أوتوماتيك لما الموديل يطلبها
)

if __name__ == "__main__":
    print("اكتب رسالة كأنك عميل. اكتب 'خروج' للإنهاء.\n")
    chat = client.chats.create(model=MODEL, config=config)
    while True:
        text = input("العميل: ").strip()
        if text in ("خروج", "exit", "quit"):
            break
        if not text:
            continue
        try:
            response = chat.send_message(text)
            print("البوت:", response.text, "\n")
        except Exception as e:
            print("⚠️ حصل خطأ:", e, "\n")
            