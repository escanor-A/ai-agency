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
6. ممنوع تماماً تذكر أرقام أو كميات المخزون للعميل (ممنوع تقول 'عندنا 50 قطعة' أو 'متاح 10 قطع')! قول 'متاح وجاهز للشحن فوراً'.
7. لو العميل سأل: "عايز أشوف صورته"، "ممكن صورة"، "وريني الألوان"، وكان الصنف له رابط صورة (image_url) راجع من الأداة: ابعتله رابط الصورة مع وصف جذاب للخامة والشياكة وشجعه يطلب!

قواعد الفروع وعناوين المحل:
1. لو العميل سأل عن مكان المحل، عنوان الفرع، أقرب فرع، أو مواعيد العمل: استدعي أداة get_branches فوراً بالمحافظة أو سيبها فاضية لو ما حددش.
2. لو رجعت فروع: ابعتله اسم الفرع، العنوان بالظبط، مواعيد العمل، ورابط اللوكيشن على الخريطة (لو متاح).
3. لو رجع إنه أونلاين بس: وضّح له بلطف إنه متجر أونلاين مع شحن لباب البيت ومعاينة قبل الاستلام.

قواعد آراء العملاء والريفيوهات (Customer Reviews & Feedback):
1. لو العميل قال كلام حلو، شكر، مدح المنتجات أو الخدمة:
   - اشكره بذوق وسجل الريفيو فوراً باستدعاء أداة submit_customer_review.
2. لو العميل طلب يسيب تقييم أو سأل: "أقيمكم فين؟"، "عايز اكتب ريفيو":
   - استدعي أداة get_google_review_link علشان تجيب رابط Google Maps / Review.
   - ابعتله الرابط المباشر بأسلوب شكر وتقدير وافرح بيه (مثلاً: هتسعدنا جداً يا فندم! ده رابط صفحتنا على خرائط جوجل تقدر تسيب تقييمك ورأيك الجميل هناك: [الرابط]).

قواعد العروض وأكواد الخصم (Promotions & Offers):
1. لو العميل سأل: "عندكم عروض؟"، "فيه خصم؟"، أو ذكر كود خصم:
   - استدعي أداة get_active_offers فوراً لمعرفة العروض المتاحة.
   - لو فيه عروض أو أكواد، وضّحها للعميل بحماس وشجعه يستفيد منها.
2. أسلوب البيع وتعلية الأوردر (Up-selling):
   - لو التاجر عامل عرض (زي شحن مجاني على قطعتين أو أكتر)، والزبون بيطلب قطعة واحدة: اقترح عليه بلطف العرض علشان يكسب الشحن المجاني!
3. عند حساب الأوردر (quote_order):
   - لو العميل معاه كود خصم، مرره في خانة promo_code علشان يتخصم في الفاتورة.

خطوات تسجيل وتعديل وإلغاء الأوردر (الدفع عند الاستلام فقط):
1. لما العميل يقرر يشتري، اجمع اللي ناقص بس: الاسم، رقم الموبايل، المحافظة، العنوان بالتفصيل.
2. نادِ quote_order علشان تجيب الإجمالي. ممنوع تحسب الأسعار أو الشحن بنفسك.
   - تنبيه هام لصيغة الأصناف (items): في أداتي quote_order و place_order، يجب دائماً وأبداً تمرير الأصناف في قائمة نصوص بصيغة ['SKU:الكمية'] مثل: ['TS-BLK-L:1'] أو ['TS-BLK-L:2', 'HD-GRY-XL:1']. كود الـ SKU بييجي دائماً من أداة check_stock.
   - كود الخصم: لو العميل معاه كود خصم، مرره في خانة promo_code عند quote_order و place_order.
3. اعرض ملخص واضح: الأصناف والكميات، سعر القطع، الشحن، الإجمالي، وإن الدفع عند الاستلام.
4. استنى العميل يأكد صراحة (زي: تمام، أكد، ماشي). ممنوع تسجّل الأوردر قبل التأكيد.
5. بعد التأكيد نادِ place_order مرة واحدة بس. ممنوع تناديها مرتين لنفس الأوردر.
6. لو العميل طلب إلغاء أوردر سابق:
   - اطلب منه رقم الأوردر ورقم الموبايل المسجل به الأوردر لتأكيد هويته (لو ما كانش كاتبهم).
   - استدعِ أداة cancel_customer_order برقم الأوردر ورقم الموبايل للتأكد من هويته ومطابقة البيانات.
   - وضّح له أن الإلغاء متاح فقط للأوردرات الجديدة (new) أو المؤكدة (confirmed) قبل خروجها للشحن.
7. لو العميل طلب تعديل عنوان الشحن أو رقم الموبايل لأوردر مسجل:
   - اطلب منه رقم الأوردر ورقم الموبايل الحالي المسجل به الأوردر لتأكيد هويته.
   - استدعِ أداة update_order_address_or_phone بالبيانات الجديدة بعد التأكد من مطابقة رقم الموبايل المسجل.
   - وضّح له أن التعديل متاح فقط للأوردرات الجديدة (new) أو المؤكدة (confirmed) قبل خروجها للشحن.

قواعد فحص الصور والأخطاء الإملائية (Images & Typo Handling):
1. لو العميل بعت صورة وسأل: "ده متاح؟"، "بكام ده؟"، "عندكم منه؟":
   - حلل الصورة كويس جداً بذكاء وحدد نوع القطعة (تيشيرت، بنطلون، هودي، سويت شيرت) ولونها.
   - نادِ check_stock فوراً باسم الصنف للتأكد من وجوده ومقاساته وسعره في المخزن.
   - رد عليه بترحيب: "أيوة يا فندم! ده [اسم الصنف]، متاح باللون ده ومقاسات كذا وسعره كذا".
2. لو العميل كتب اسم الصنف غلط أو بحروف عامية (زي: تشيرت، سوتشيرت، كارجو، هودى):
   - افهم الكلمة المقصودة بذكاء من كتالوج المحل، ونادِ check_stock بالاسم الصحيح مباشرة.
"""

# إدارة ذاكرة المحادثات في RAM بحد أقصى لمنع تسريب الذاكرة (Memory Leak Prevention)
CUSTOMER_CHATS = {}
MAX_ACTIVE_CHATS = 500
CHAT_INACTIVITY_TTL = 3600  # ساعة واحدة صلاحية الجلسة في الرام


def cleanup_customer_chats():
    """تنظيف الجلسات المنتهية وغير النشطة من الرام لمنع تضخم الذاكرة"""
    now = time.time()
    expired = [k for k, v in CUSTOMER_CHATS.items() if now - v.get("last_active", 0) > CHAT_INACTIVITY_TTL]
    for k in expired:
        CUSTOMER_CHATS.pop(k, None)
    if len(CUSTOMER_CHATS) > MAX_ACTIVE_CHATS:
        sorted_keys = sorted(CUSTOMER_CHATS.keys(), key=lambda k: CUSTOMER_CHATS[k].get("last_active", 0))
        for k in sorted_keys[:MAX_ACTIVE_CHATS // 5]:
            CUSTOMER_CHATS.pop(k, None)


def get_or_create_conversation(tenant_id: str, customer_uuid: str, channel: str) -> str:
    """جلب أو إنشاء محادثة للعميل في جدول conversations لحفظ سياق الشات في قاعدة البيانات"""
    if not tenant_id or not customer_uuid:
        return None
    try:
        rows = (
            supabase.table("conversations")
            .select("id")
            .eq("tenant_id", tenant_id)
            .eq("customer_id", customer_uuid)
            .eq("channel", channel)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if rows:
            return rows[0]["id"]

        new_row = {
            "tenant_id": tenant_id,
            "customer_id": customer_uuid,
            "channel": channel,
            "mode": "bot",
        }
        res = supabase.table("conversations").insert(new_row).execute()
        if res.data:
            return res.data[0]["id"]
    except Exception as e:
        print("خطأ في جلب أو إنشاء سجل المحادثة بالداتابيز:", e)
    return None


def load_recent_messages(conversation_id: str, limit: int = 14) -> list[types.Content]:
    """جلب الرسائل السابقة للمحادثة من قاعدة البيانات لإعادة بناء سياق الشات في Gemini بعد إعادة التشغيل أو النشر"""
    if not conversation_id:
        return []
    try:
        rows = (
            supabase.table("messages")
            .select("role,content,created_at")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        if not rows:
            return []

        rows.reverse()
        history = []
        for r in rows:
            role = "user" if r["role"] == "customer" else "model"
            history.append(types.Content(
                role=role,
                parts=[types.Part.from_text(text=r["content"])]
            ))

        # التأكد من التناوب الصارم user -> model المطلوب في Gemini
        clean_history = []
        last_role = None
        for item in history:
            if item.role != last_role:
                clean_history.append(item)
                last_role = item.role

        # قاعدة Gemini: يجب أن تبدأ المحادثة دائماً بـ user
        if clean_history and clean_history[0].role != "user":
            clean_history = clean_history[1:]

        # وقاعدة Gemini: يجب أن تنتهي المحادثة في history بـ model حتى يتم إرسال رسالة user الجديدة بعدها
        if clean_history and clean_history[-1].role != "model":
            clean_history = clean_history[:-1]

        return clean_history
    except Exception as e:
        print("خطأ في استرجاع سياق المحادثة من الداتابيز:", e)
        return []


def save_message_to_db(conversation_id: str, role: str, content: str):
    """حفظ رسالة في جدول messages في قاعدة البيانات (role: 'customer' | 'assistant')"""
    if not conversation_id or not content or not content.strip():
        return
    db_role = "customer" if role in ("customer", "user") else "assistant"
    try:
        supabase.table("messages").insert({
            "conversation_id": conversation_id,
            "role": db_role,
            "content": content.strip()[:4000],
        }).execute()
        supabase.table("conversations").update({"updated_at": "now()"}).eq("id", conversation_id).execute()
    except Exception as e:
        print("خطأ في حفظ الرسالة بالداتابيز:", e)


def handle_customer_message(tenant_id: str, customer_id: str, text: str = "", image_bytes: bytes = None, image_mime_type: str = "image/jpeg", channel: str = "messenger") -> str:
    """معالجة رسالة عميل (نصية أو صورة) جاية من أي منصة (واتساب، ماسنجر، إنستا، تيك توك) وحفظ سياق الشات"""
    
    # تعريف أدوات الموديل الخاصة بهذا التاجر تحديداً (عزل تام)
    def check_stock(product_name: str, size: str = "", color: str = "") -> dict:
        """فحص توافر المنتج في المخزن ومعرفة الألوان والمقاسات والأسعار وأكواد الصنف (sku) المتاحة. لو الصنف خلصان بترجع بدائل متاحة.
        
        Args:
            product_name: اسم المنتج أو نوعه (مثلاً: تيشيرت، هودي، بنطلون)
            size: المقاس بالحروف الإنجليزية: M أو L أو XL (سيبه فاضي لو العميل ماحددش)
            color: اللون بالعربي: أسود أو أبيض أو رمادي أو كحلي (سيبه فاضي لو العميل ماحددش)
            
        Returns:
            dict يحتوي على الأصناف المتاحة مع كود الصنف (sku) والسعر والكمية.
        """
        return db_check_stock(tenant_id, product_name, size=size or None, color=color or None)

    def quote_order(items: list[str], governorate: str, promo_code: str = "") -> dict:
        """حساب سعر الأوردر والشحن والإجمالي وتطبيق كود الخصم وعروض الشحن المجاني من غير ما تسجل أوردر. يجب استدعاؤها قبل عرض الملخص على العميل.
        
        Args:
            items: قائمة الأصناف بصيغة إجبارية دقيقة: ['SKU:الكمية'] (مثال: ['TS-BLK-L:1'] أو ['TS-BLK-L:2', 'HD-GRY-XL:1']) حيث كود الصنف (sku) يأتي من أداة check_stock.
            governorate: اسم المحافظة بالعربي لحساب مصاريف الشحن (مثال: 'القاهرة', 'الجيزة', 'الإسكندرية').
            promo_code: كود الخصم إذا ذكره العميل للاستفادة من التخفيض (اختياري).
            
        Returns:
            dict يحتوي على تفاصيل الأسعار، الخصم، الشحن، والإجمالي النهائي وطريقة الدفع.
        """
        return orders.quote_order(tenant_id, items, governorate, promo_code=promo_code)

    def get_active_offers() -> dict:
        """بترجّع كل العروض وأكواد الخصم النشطة حالياً للمحل (عروض الشحن المجاني، نسب الخصم، أكواد التخفيض)."""
        offers = orders.get_active_promotions(tenant_id)
        if not offers:
            return {"status": "no_offers", "message": "حالياً مفيش عروض إضافية، بس أسعارنا ممتازة وشحن سريع ومعاينة قبل الاستلام!"}
        return {"status": "ok", "offers": offers}

    def place_order(customer_name: str, phone: str, governorate: str, address: str, items: list[str], promo_code: str = "") -> dict:
        """تسجيل الأوردر نهائياً في النظام وخصم المخزون وإشعار التاجر فوراً (يُستدعى فقط بعد أن يؤكد العميل الملخص صراحة).
        
        Args:
            customer_name: اسم العميل ثلاثي أو ثنائي.
            phone: رقم موبايل العميل (11 رقم مصري يبدأ بـ 01).
            governorate: محافظة العميل (مثال: 'القاهرة').
            address: العنوان بالتفصيل (المنطقة، الشارع، رقم العمارة والشقة أو علامة مميزة).
            items: قائمة الأصناف بصيغة إجبارية دقيقة: ['SKU:الكمية'] (مثال: ['TS-BLK-L:1']) المستخرجة من أداة check_stock.
            promo_code: كود الخصم المطبق إن وجد لخصمه من الأوردر (اختياري).
            
        Returns:
            dict يحتوي على حالة الأوردر ورقم الأوردر المسجل وتفاصيل التأكيد.
        """
        return orders.place_order(
            tenant_id, customer_name, phone, governorate, address, items,
            channel=channel, channel_user_id=customer_id, promo_code=promo_code
        )

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

    def cancel_customer_order(order_number: int, phone: str = "", reason: str = "") -> dict:
        """إلغاء أوردر مسجل سابقاً بناءً على طلب العميل بعد التأكد من ملكيته للأوردر ومطابقة رقم الموبايل.
        
        Args:
            order_number: رقم الأوردر المراد إلغاؤه (مثلاً 5)
            phone: رقم الموبايل المسجل به الأوردر لتأكيد هوية العميل
            reason: سبب الإلغاء إذا ذكره العميل
        """
        customer_uuid = orders.get_or_create_customer(tenant_id, channel, customer_id)

        rows = (
            supabase.table("orders")
            .select("id,status,customer_id,phone,customer_name")
            .eq("tenant_id", tenant_id)
            .eq("order_number", int(order_number))
            .execute()
            .data
        )
        if not rows:
            return {"status": "error", "reason": f"الأوردر #{order_number} مش موجود"}

        ord_data = rows[0]

        # 1. السماح بالإلغاء في حالة new و confirmed بس
        if ord_data["status"] not in ("new", "confirmed"):
            if ord_data["status"] == "cancelled":
                return {"status": "error", "reason": f"الأوردر #{order_number} ملغي بالفعل"}
            return {"status": "error", "reason": f"لا يمكن إلغاء الأوردر #{order_number} لأن حالته الحالية ({ord_data['status']}) — مسموح بالإلغاء فقط قبل خروج الشحنة"}

        # 2. التحقق من تطابق هوية العميل المسجلة على الأوردر
        if ord_data.get("customer_id") and customer_uuid and ord_data["customer_id"] != customer_uuid:
            return {"status": "error", "reason": "عذراً، الأوردر ده مش مسجل بنفس هذا الحساب ولا يمكن إلغاؤه"}

        # 3. مطابقة رقم الموبايل
        expected_phone = orders.normalize_phone(ord_data.get("phone") or "")
        input_phone = orders.normalize_phone(phone) if phone else None
        if not input_phone:
            # محاولة قراءة الرقم من customer_id لو الشات من واتساب
            input_phone = orders.normalize_phone(customer_id)

        if not input_phone:
            return {"status": "error", "reason": "يرجى كتابة رقم الموبايل المسجل به الأوردر لتأكيد هويتك وإتمام الإلغاء"}

        if expected_phone and input_phone != expected_phone:
            return {"status": "error", "reason": "رقم الموبايل غير مطابق لبيانات الأوردر المسجلة"}

        return orders.cancel_order(tenant_id, order_number, reason=reason)

    def update_order_address_or_phone(order_number: int, customer_phone: str = "", new_address: str = "", new_phone: str = "", phone: str = "", address: str = "") -> dict:
        """تعديل عنوان الشحن أو رقم الموبايل لأوردر قائم بناءً على طلب العميل بعد التأكد من ملكيته للأوردر ومطابقة رقم الموبايل.
        
        Args:
            order_number: رقم الأوردر المراد تعديله
            customer_phone: رقم الموبايل الحالي المسجل به الأوردر لتأكيد هوية العميل
            new_address: العنوان الجديد بالتفصيل إن وجد
            new_phone: رقم الموبايل الجديد إن كان العميل يريد تغييره
            phone: حقل بديل لرقم الموبايل
            address: حقل بديل للعنوان الجديد
        """
        customer_uuid = orders.get_or_create_customer(tenant_id, channel, customer_id)

        rows = (
            supabase.table("orders")
            .select("id,status,customer_id,phone,address,customer_name")
            .eq("tenant_id", tenant_id)
            .eq("order_number", int(order_number))
            .execute()
            .data
        )
        if not rows:
            return {"status": "error", "reason": f"الأوردر #{order_number} مش موجود"}

        ord_data = rows[0]

        # 1. السماح بالتعديل في حالة new و confirmed بس
        if ord_data["status"] not in ("new", "confirmed"):
            return {"status": "error", "reason": f"لا يمكن تعديل الأوردر #{order_number} لأن حالته الحالية ({ord_data['status']}) — مسموح بالتعديل فقط قبل خروج الشحنة"}

        # 2. التحقق من تطابق هوية العميل المسجلة على الأوردر
        if ord_data.get("customer_id") and customer_uuid and ord_data["customer_id"] != customer_uuid:
            return {"status": "error", "reason": "عذراً، الأوردر ده مش مسجل بنفس هذا الحساب ولا يمكن تعديله"}

        # 3. مطابقة رقم الموبايل الحالي المسجل به الأوردر
        expected_phone = orders.normalize_phone(ord_data.get("phone") or "")
        check_phone = None
        if customer_phone:
            check_phone = orders.normalize_phone(customer_phone)
        elif phone and expected_phone and orders.normalize_phone(phone) == expected_phone:
            check_phone = orders.normalize_phone(phone)
        elif orders.normalize_phone(customer_id):
            check_phone = orders.normalize_phone(customer_id)

        if not check_phone:
            return {"status": "error", "reason": "يرجى كتابة رقم الموبايل المسجل به الأوردر لتأكيد هويتك وإتمام التعديل"}

        if expected_phone and check_phone != expected_phone:
            return {"status": "error", "reason": "رقم الموبايل غير مطابق لبيانات الأوردر المسجلة"}

        # تحديد العنوان الجديد ورقم الموبايل الجديد
        target_addr = (new_address or address or "").strip()
        target_phone = ""
        if new_phone:
            target_phone = new_phone.strip()
        elif phone and orders.normalize_phone(phone) != expected_phone:
            target_phone = phone.strip()

        if not target_addr and not target_phone:
            return {"status": "error", "reason": "لم يتم تحديد عنوان أو رقم موبايل جديد لتعديله"}

        return orders.update_order_shipping(tenant_id, order_number, phone=target_phone, address=target_addr)

    session_key = f"{tenant_id}:{channel}:{customer_id}"
    now = time.time()

    # 1. تنظيف الذاكرة في RAM إذا زاد عدد الجلسات عن الحد الأقصى (LRU / TTL)
    cleanup_customer_chats()

    # 2. تحديد هوية العميل وسجل المحادثة في قاعدة البيانات
    cust_uuid = orders.get_or_create_customer(tenant_id, channel, customer_id)
    conv_id = get_or_create_conversation(tenant_id, cust_uuid, channel) if cust_uuid else None

    # 3. إنشاء أو استرجاع الجلسة
    if session_key not in CUSTOMER_CHATS:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[
                check_stock, quote_order, place_order, get_branches,
                submit_customer_review, get_google_review_link, get_active_offers,
                cancel_customer_order, update_order_address_or_phone
            ],
        )
        # استرجاع سياق الرسائل السابقة من الداتابيز حتى لو حصل إعادة تشغيل أو نشر جديد على Render
        history = load_recent_messages(conv_id, limit=14) if conv_id else []
        chat = client.chats.create(model=MODEL, config=config, history=history or None)
        CUSTOMER_CHATS[session_key] = {
            "chat": chat,
            "last_active": now,
            "conversation_id": conv_id,
        }
    else:
        CUSTOMER_CHATS[session_key]["last_active"] = now
        chat = CUSTOMER_CHATS[session_key]["chat"]

    contents_to_send = []
    if image_bytes:
        contents_to_send.append(types.Part.from_bytes(data=bytes(image_bytes), mime_type=image_mime_type))
    if text and text.strip():
        contents_to_send.append(text.strip())
    elif not contents_to_send:
        contents_to_send.append("أهلاً بحضرتك")

    message_payload = contents_to_send if len(contents_to_send) > 1 else contents_to_send[0]

    # حفظ رسالة العميل في الداتابيز
    if conv_id and text and text.strip():
        save_message_to_db(conv_id, "customer", text.strip())

    for attempt in range(3):
        try:
            res = chat.send_message(message_payload)
            reply = res.text or "تمام يا فندم، أقدر أساعدك بإيه تاني؟"
            # حفظ رد البوت في الداتابيز لضمان استمرارية السياق
            if conv_id:
                save_message_to_db(conv_id, "assistant", reply)
            return reply
        except Exception as e:
            if ("503" in str(e) or "429" in str(e)) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"Error handling customer {customer_id}:", e)
            return "يا فندم ثواني بس وراجع لحضرتك فوراً 🙏"
