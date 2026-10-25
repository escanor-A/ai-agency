import os
import sys
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types

import orders
import notify
from check_stock import check_stock as db_check_stock
from check_stock import supabase

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-3.5-flash-lite"

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

قواعد الفروع وعناوين المحل:
1. لو العميل سأل عن مكان المحل، عنوان الفرع، أقرب فرع، أو مواعيد العمل: استدعي أداة get_branches فوراً بالمحافظة أو سيبها فاضية لو ما حددش.
2. لو رجعت فروع: ابعتله اسم الفرع، العنوان بالظبط، مواعيد العمل، ورابط اللوكيشن على الخريطة (لو متاح).
3. لو رجع إنه أونلاين بس: وضّح له بلطف إنه متجر أونلاين مع شحن لباب البيت ومعاينة قبل الاستلام.

قواعد آراء العملاء والريفيوهات (Customer Reviews):
1. لو العميل بيشكر في اللبس أو الخامة أو وصله الأوردر وعجبه، أو بيعبر عن امتنانه ورأيه الإيجابي:
   - استدعي أداة submit_customer_review فوراً بنص كلامه ورأيه علشان تتبعت لصاحب المحل على تليجرام.
   - رد عليه بحرارة وأسلوب مصري ودود ومبهج جداً (مثلاً: ألف شكر على كلامك الجميل.. تعيش وتلبس وتتهنى بيه يا رب، فرحتنا برأيك ومنورنا دايماً!).

قواعد تقييمات وريفيو جوجل (Google Review):
1. لو العميل سأل: "عايز أكتب ريفيو على جوجل"، "فين رابط تقييم جوجل"، "إزاي أقيمكم على جوجل ماب":
   - استدعي أداة get_google_review_link فوراً.
   - ابعتله الرابط المباشر بأسلوب شكر وتقدير وافرح بيه (مثلاً: هتسعدنا جداً يا فندم! ده رابط صفحتنا على خرائط جوجل تقدر تسيب تقييمك ورأيك الجميل هناك: [الرابط]).

قواعد العروض وأكواد الخصم (Promotions & Offers):
1. لو العميل سأل: "عندكم عروض؟"، "فيه خصم؟"، أو ذكر كود خصم:
   - استدعي أداة get_active_offers فوراً لمعرفة العروض المتاحة.
   - لو فيه عروض أو أكواد، وضّحها للعميل بحماس وشجعه يستفيد منها.
2. أسلوب البيع وتعلية الأوردر (Up-selling):
   - لو التاجر عامل عرض (زي شحن مجاني على قطعتين أو أكتر)، والزبون بيطلب قطعة واحدة: اقترح عليه بلطف العرض علشان يكسب الشحن المجاني!
3. عند حساب الأوردر (quote_order):
   - لو العميل معاه كود خصم، مرره في خانة promo_code علشان يتخصم في الفاتورة.

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

    def quote_order(items: list[str], governorate: str, promo_code: str = "") -> dict:
        return orders.quote_order(tenant_id, items, governorate, promo_code=promo_code)

    def get_active_offers() -> dict:
        """بترجّع كل العروض وأكواد الخصم النشطة حالياً للمحل (عروض الشحن المجاني، نسب الخصم، أكواد التخفيض)."""
        offers = orders.get_active_promotions(tenant_id)
        if not offers:
            return {"status": "no_offers", "message": "حالياً مفيش عروض إضافية، بس أسعارنا ممتازة وشحن سريع ومعاينة قبل الاستلام!"}
        return {"status": "ok", "offers": offers}

    def place_order(customer_name: str, phone: str, governorate: str, address: str, items: list[str]) -> dict:
        return orders.place_order(tenant_id, customer_name, phone, governorate, address, items)

    def get_branches(governorate: str = "") -> dict:
        """بترجّع فروع ومحلات البراند، العناوين، مواعيد العمل، ورابط اللوكيشن على الخريطة.
        
        Args:
            governorate: اسم المحافظة أو المنطقة اللي العميل بيسأل عنها (سيبها فاضية لو بيسأل عن كل الفروع)
        """
        query = supabase.table("branches").select("name, governorate, city, address, google_maps_url, working_hours, phone").eq("tenant_id", tenant_id)
        if governorate:
            query = query.ilike("governorate", f"%{governorate.strip()}%")
        rows = query.execute().data
        if not rows:
            # لو مفيش فروع مسجلة، نرجع إنه متجر أونلاين بشحن فوري
            return {
                "status": "online_only",
                "message": "إحنا متجر أونلاين وشغالين شحن لجميع محافظات مصر لحد باب البيت، وتقدر تعاين الشحنة قبل الاستلام!"
            }
        return {"status": "ok", "branches": rows}

    def submit_customer_review(feedback: str, rating: int = 5, reviewer_name: str = "") -> dict:
        """تسجيل رأي أو ريفيو أو شكر من العميل وإشعار صاحب المحل فوراً على تليجرام.
        
        Args:
            feedback: نص كلام العميل ورأيه بالظبط
            rating: تقييم العميل من 1 إلى 5 نجوم (افتراضي 5)
            reviewer_name: اسم العميل لو قاله في الشات (اختياري)
        """
        name = reviewer_name or customer_id
        notify.send_review_notification(tenant_id, name, feedback, rating)
        try:
            supabase.table("reviews").insert({
                "tenant_id": tenant_id,
                "customer_id": name,
                "feedback": feedback,
                "rating": rating
            }).execute()
        except Exception:
            pass
        return {"status": "ok", "message": "تم إشعار التاجر وتسجيل الريفيو بنجاح"}

    def get_google_review_link(branch_name: str = "") -> dict:
        """بترجّع رابط التقييم على خرائط جوجل (Google Review / Maps) علشان العميل يسيب ريفيو للمحل.
        
        Args:
            branch_name: اسم الفرع أو المنطقة لو العميل حدد فرع معين (اختياري)
        """
        query = supabase.table("branches").select("name, google_maps_url, address").eq("tenant_id", tenant_id)
        if branch_name:
            query = query.ilike("name", f"%{branch_name.strip()}%")
        rows = query.execute().data
        
        valid = [r for r in rows if r.get("google_maps_url")]
        if valid:
            b = valid[0]
            return {
                "status": "ok",
                "branch_name": b["name"],
                "url": b["google_maps_url"],
                "address": b["address"],
            }
        return {
            "status": "not_available",
            "message": "حالياً مفيش رابط مباشر لخرائط جوجل مسجل، بس تقدر تسيب رأيك الجميل هنا في الشات وهيوصل فوراً لصاحب المحل!"
        }

    session_key = f"{tenant_id}:{customer_id}"
    if session_key not in CUSTOMER_CHATS:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[check_stock, quote_order, place_order, get_branches, submit_customer_review, get_google_review_link, get_active_offers],
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
