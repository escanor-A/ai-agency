import os
import sys
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types

import orders
from check_stock import check_stock as db_check_stock
from check_stock import supabase

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-3.5-flash"

SYSTEM_PROMPT = """أنت بياع شاطر في براند مصري، بترد على العملاء على السوشيال ميديا (واتساب، فيسبوك ماسنجر، إنستجرام، تيك توك).

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
"""

# ذاكرة بسيطة للمحادثات (لكل عميل session مستقلة)
CUSTOMER_CHATS = {}


def handle_customer_message(tenant_id: str, customer_id: str, text: str) -> str:
    """معالجة رسالة عميل جاية من أي منصة (واتساب، ماسنجر، إنستا، تيك توك) وحفظ سياق الشات"""
    
    # تعريف أدوات الموديل الخاصة بهذا التاجر تحديداً (عزل تام)
    def check_stock(product_name: str, size: str = "", color: str = "") -> dict:
        return db_check_stock(tenant_id, product_name, size=size or None, color=color or None)

    def quote_order(items: list[str], governorate: str) -> dict:
        return orders.quote_order(tenant_id, items, governorate)

    def place_order(customer_name: str, phone: str, governorate: str, address: str, items: list[str]) -> dict:
        return orders.place_order(tenant_id, customer_name, phone, governorate, address, items)

    session_key = f"{tenant_id}:{customer_id}"
    if session_key not in CUSTOMER_CHATS:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[check_stock, quote_order, place_order],
        )
        CUSTOMER_CHATS[session_key] = client.chats.create(model=MODEL, config=config)

    chat = CUSTOMER_CHATS[session_key]
    for attempt in range(3):
        try:
            res = chat.send_message(text)
            return res.text or "تمام يا فندم، أقدر أساعدك بإيه تاني؟"
        except Exception as e:
            if ("503" in str(e) or "429" in str(e)) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"Error handling customer {customer_id}:", e)
            return "يا فندم ثواني بس وراجع لحضرتك فوراً 🙏"
