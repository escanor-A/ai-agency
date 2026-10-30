import json
import os
import sys
import threading
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types

import orders
import notify
from check_stock import check_stock as db_check_stock
from check_stock import escape_like, supabase, _norm_ar

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
7. لو العميل سأل: "عايز أشوف صورته"، "ممكن صورة"، "وريني الألوان"، وكان الصنف له صورة (image_url غير فاضي) راجعة من check_stock: نادِ send_product_image بكود الصنف (sku) وهتتبعت له الصورة كصورة فعلية تلقائياً بعد ردك. ممنوع تكتب رابط الصورة في الرد. اكتب وصف جذاب للخامة والشياكة وقوله "صورته وصلتلك تحت" وشجعه يطلب! لو الأداة رجعت no_image اعتذر بلطف إن مفيش صورة حالياً. (الأداة متاحة في الشات الخاص بس، مش في التعليقات.)
8. خامات المنتجات (Material):
   - لو العميل سأل عن الخامة (مثل: "الخامة إيه؟"، "ده قطن ولا بوليستر؟"، "خامته تقيلة ولا صيفي؟"، "الخامة نوعها إيه؟"):
     استخرج نوع الخامة من حقل material الراجع من أداة check_stock واشرح له مميزاتها بأسلوب بياع شاطر (مثلاً: قطن 100% بيور ناعم على الجسم ومش بيكش، أو ميلتون مبطن ومريح، أو وتربروف، أو كتان/جبردين).
     لو الصنف معندوش خامة مسجلة، طمّن العميل إنها خامة ممتازة ومريحة، وإن المعاينة والقياس متاحة ومجانية قبل دفع أي مليم مع المندوب.

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
   - عروض قسائم الشراء وكروت الهدايا (Vouchers / Gift Cards): لو التاجر عامل عرض زي (اشتري بقيمة 1000 ج واحصل على فاوتشر/كارت هدايا بـ 200 ج لطلبك القادم):
     وضحه للعميل وشجعه يزود سلة مشترياته ليصل للحد الأدنى ويكسب الهدية. ولو كان الأوردر مؤهل، بشره في الملخص إنه كسب كارت هدايا لطلبه القادم!
3. عند حساب الأوردر (quote_order) أو تسجيله (place_order):
   - لو العميل معاه كود خصم، مرره في خانة promo_code علشان يتخصم في الفاتورة.
   - لو رجع promo_error من الأداة: لازم تبلّغ العميل فوراً وبوضوح إن كود الخصم غير صالح أو منتهي بنص الخطأ (promo_error) واعرض عليه الفاتورة والإجمالي بدونه بذوق.

خطوات تسجيل وتعديل وإلغاء الأوردر (الدفع عند الاستلام فقط):
1. لما العميل يقرر يشتري، اجمع اللي ناقص بس: الاسم، رقم الموبايل، المحافظة، العنوان بالتفصيل.
2. نادِ quote_order علشان تجيب الإجمالي. ممنوع تحسب الأسعار أو الشحن بنفسك.
   - تنبيه هام لصيغة الأصناف (items): في أداتي quote_order و place_order، يجب دائماً وأبداً تمرير الأصناف في قائمة نصوص بصيغة ['SKU:الكمية'] مثل: ['TS-BLK-L:1'] أو ['TS-BLK-L:2', 'HD-GRY-XL:1']. كود الـ SKU بييجي دائماً من أداة check_stock.
   - كود الخصم: لو العميل معاه كود خصم، مرره في خانة promo_code عند quote_order و place_order.
3. اعرض ملخص واضح: الأصناف والكميات، سعر القطع، الشحن، الإجمالي، وإن الدفع عند الاستلام.
4. استنى العميل يأكد صراحة (زي: تمام، أكد، ماشي). ممنوع تسجّل الأوردر قبل التأكيد.
5. بعد التأكيد نادِ place_order مرة واحدة بس. ممنوع تناديها مرتين لنفس الأوردر.
   - تنبيه حاسم: لو أداة place_order رجعت status: error، ممنوع تماماً أن تدعي أن الأوردر تم تسجيله أو تبارك للعميل! يجب إخبار العميل بوجود مشكلة بالسبب المذكور واطلب منه مراجعة بياناته برفق.
   - فقط عندما ترجع place_order حالة status: ok بنجاح، تهنئ العميل وتذكر له تفاصيل طلبه وتؤكد له وصول الأوردر.
6. لو العميل طلب إلغاء أوردر سابق:
   - اطلب منه رقم الأوردر ورقم الموبايل المسجل به الأوردر لتأكيد هويته (لو ما كانش كاتبهم).
   - استدعِ أداة cancel_customer_order برقم الأوردر ورقم الموبايل للتأكد من هويته ومطابقة البيانات.
   - وضّح له أن الإلغاء متاح فقط للأوردرات الجديدة (new) أو المؤكدة (confirmed) قبل خروجها للشحن.
7. لو العميل طلب تعديل عنوان الشحن أو رقم الموبايل لأوردر مسجل:
   - اطلب منه رقم الأوردر ورقم الموبايل الحالي المسجل به الأوردر لتأكيد هويته.
   - استدعِ أداة update_order_address_or_phone ممرراً رقم الأوردر (order_number) ورقم الموبايل الحالي في current_phone، مع العنوان الجديد في address و/أو رقم الموبايل الجديد في phone.
   - وضّح له أن التعديل متاح فقط للأوردرات الجديدة (new) أو المؤكدة (confirmed) قبل خروجها للشحن.

قواعد فحص الصور والأخطاء الإملائية (Images & Typo Handling):
1. لو العميل بعت صورة وسأل: "ده متاح؟"، "بكام ده؟"، "عندكم منه؟":
   - حلل الصورة كويس جداً بذكاء وحدد نوع القطعة (تيشيرت، بنطلون، هودي، سويت شيرت) ولونها.
   - نادِ check_stock فوراً باسم الصنف للتأكد من وجوده ومقاساته وسعره في المخزن.
   - رد عليه بترحيب: "أيوة يا فندم! ده [اسم الصنف]، متاح باللون ده ومقاسات كذا وسعره كذا".
2. لو العميل كتب اسم الصنف غلط أو بحروف عامية (زي: تشيرت، سوتشيرت، كارجو، هودى):
   - افهم الكلمة المقصودة بذكاء من كتالوج المحل، ونادِ check_stock بالاسم الصحيح مباشرة.
"""

# إدارة ذاكرة المحادثات في RAM بحد أقصى لمنع تسريب الذاكرة وقفل للتزامن
CUSTOMER_CHATS = {}
CHATS_DICT_LOCK = threading.Lock()
MAX_ACTIVE_CHATS = 500
CHAT_INACTIVITY_TTL = 3600  # ساعة واحدة صلاحية الجلسة في الرام


class RateLimiter:
    """حد معدل: لكل (tenant, sender) 8 رسائل في الدقيقة، وسقف يومي منفصل للمحادثات والتعليقات"""
    def __init__(self, per_minute_limit: int = 8):
        self.lock = threading.RLock()
        self.per_minute_limit = per_minute_limit
        self.sender_timestamps = {}  # (tenant_id, sender_id) -> [timestamps]
        self.tenant_daily_counts = {}  # tenant_id -> {"date": "YYYY-MM-DD", "count": int}
        self.tenant_daily_comments = {}  # tenant_id -> {"date": "YYYY-MM-DD", "count": int}
        self.last_warned = {}  # (tenant_id, sender_id) -> float
        self.last_cleanup = time.time()

    def check_rate_limit(self, tenant_id: str, sender_id: str) -> tuple[bool, str]:
        with self.lock:
            now = time.time()
            self.maybe_cleanup(now)

            key = (str(tenant_id), str(sender_id))

            # 1. سقف يومي لكل تاجر من متغير بيئة
            daily_cap_str = os.environ.get("TENANT_DAILY_MESSAGE_CAP") or os.environ.get("DAILY_TENANT_MSG_CAP") or "1000"
            try:
                daily_cap = int(daily_cap_str)
            except ValueError:
                daily_cap = 1000

            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            daily_info = self.tenant_daily_counts.get(tenant_id)
            if not daily_info or daily_info.get("date") != today:
                daily_info = {"date": today, "count": 0}
                self.tenant_daily_counts[tenant_id] = daily_info

            if daily_cap > 0 and daily_info["count"] >= daily_cap:
                last_w = self.last_warned.get(key, 0.0)
                if now - last_w >= 60.0:
                    self.last_warned[key] = now
                    return False, "يا فندم المتجر وصل للحد الأقصى اليومي للرسائل حالياً. هيتواصل مع حضرتك أحد ممثلي خدمة العملاء قريباً 🙏"
                return False, ""

            # 2. حد 8 رسائل في الدقيقة لكل (tenant, sender): رد مرة واحدة كل دقيقة ثم تجاهل
            timestamps = self.sender_timestamps.get(key, [])
            window_start = now - 60.0
            timestamps = [ts for ts in timestamps if ts > window_start]

            if len(timestamps) >= self.per_minute_limit:
                self.sender_timestamps[key] = timestamps
                last_w = self.last_warned.get(key, 0.0)
                if now - last_w >= 60.0:
                    self.last_warned[key] = now
                    return False, "يا فندم فضلاً الانتظار لحظات بدون تكرار إرسال الرسائل علشان أقدر أرد على استفسارك بدقة 🙏"
                return False, ""

            timestamps.append(now)
            self.sender_timestamps[key] = timestamps
            daily_info["count"] += 1
            return True, ""

    def check_comment_rate_limit(self, tenant_id: str, sender_id: str) -> tuple[bool, str]:
        """سقف يومي منفصل للتعليقات بحيث لا يستنفد سقف المحادثات DMs"""
        with self.lock:
            now = time.time()
            self.maybe_cleanup(now)

            # سقف التعليقات اليومي من متغير البيئة TENANT_DAILY_COMMENTS_CAP (افتراضي 1000)
            daily_cap_str = os.environ.get("TENANT_DAILY_COMMENTS_CAP") or os.environ.get("DAILY_TENANT_COMMENTS_CAP") or "1000"
            try:
                daily_cap = int(daily_cap_str)
            except ValueError:
                daily_cap = 1000

            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            daily_info = self.tenant_daily_comments.get(tenant_id)
            if not daily_info or daily_info.get("date") != today:
                daily_info = {"date": today, "count": 0}
                self.tenant_daily_comments[tenant_id] = daily_info

            if daily_cap > 0 and daily_info["count"] >= daily_cap:
                return False, "سقف التعليقات اليومي للمتجر مكتمل حالياً"

            # حد الدقيقة لكل مستخدم في التعليقات
            key = (str(tenant_id), str(sender_id))
            timestamps = self.sender_timestamps.get(key, [])
            window_start = now - 60.0
            timestamps = [ts for ts in timestamps if ts > window_start]
            if len(timestamps) >= self.per_minute_limit:
                return False, "تجاوز حد التعليقات في الدقيقة"

            timestamps.append(now)
            self.sender_timestamps[key] = timestamps
            daily_info["count"] += 1
            return True, ""

    def maybe_cleanup(self, now: float):
        if now - self.last_cleanup >= 300:
            self.cleanup(now)

    def cleanup(self, now: float = None):
        with self.lock:
            if now is None:
                now = time.time()
            self.last_cleanup = now
            window_start = now - 60.0
            expired_keys = []
            for key, ts_list in self.sender_timestamps.items():
                valid_ts = [ts for ts in ts_list if ts > window_start]
                if not valid_ts:
                    expired_keys.append(key)
                else:
                    self.sender_timestamps[key] = valid_ts
            for k in expired_keys:
                self.sender_timestamps.pop(k, None)

            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for tid in list(self.tenant_daily_counts.keys()):
                if self.tenant_daily_counts[tid].get("date") != today:
                    self.tenant_daily_counts.pop(tid, None)

            for tid in list(self.tenant_daily_comments.keys()):
                if self.tenant_daily_comments[tid].get("date") != today:
                    self.tenant_daily_comments.pop(tid, None)

            for k in list(self.last_warned.keys()):
                if now - self.last_warned[k] > 60.0:
                    self.last_warned.pop(k, None)


RATE_LIMITER = RateLimiter()


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


def purge_old_chat_messages(days: int = 20) -> dict:
    """
    حذف رسائل الشات القديمة التي مضى عليها أكثر من 20 يوماً من جدول messages 
    وتنظيف المحادثات القديمة لتقليل استهلاك مساحة قاعدة البيانات (Supabase Storage).
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    deleted_messages = 0
    deleted_conversations = 0
    try:
        # 1. حذف الرسائل التي مر عليها أكثر من 20 يوماً
        res = supabase.table("messages").delete().lt("created_at", cutoff).execute()
        if res and hasattr(res, "data") and res.data:
            deleted_messages = len(res.data)
            print(f"🧹 [تنظيف الشات] تم حذف {deleted_messages} رسالة قديمة مضى عليها أكثر من {days} يوماً.")
        
        # 2. تنظيف المحادثات القديمة التي ليس لها نشاط حديث
        conv_res = supabase.table("conversations").delete().lt("updated_at", cutoff).execute()
        if conv_res and hasattr(conv_res, "data") and conv_res.data:
            deleted_conversations = len(conv_res.data)
            print(f"🧹 [تنظيف الشات] تم تنظيف {deleted_conversations} محادثة قديمة.")
            
        return {
            "status": "ok",
            "days": days,
            "cutoff": cutoff,
            "deleted_messages": deleted_messages,
            "deleted_conversations": deleted_conversations,
        }
    except Exception as e:
        print(f"❌ خطأ أثناء تنظيف الشاتات القديمة: {e}")
        return {"status": "error", "error": str(e)}


def cancel_customer_order_handler(
    tenant_id: str, channel: str, customer_id: str, order_number: int, phone: str = "", reason: str = ""
) -> dict:
    """إلغاء أوردر مسجل سابقاً بعد التحقق من الملكية ومطابقة البيانات (Fail-Closed)."""
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

    # 2. التحقق من تطابق هوية العميل (Fail-closed)
    if ord_data.get("customer_id"):
        if not customer_uuid or ord_data["customer_id"] != customer_uuid:
            return {"status": "error", "reason": "عذراً، الأوردر ده مش مسجل بنفس هذا الحساب ولا يمكن إلغاؤه"}
    else:
        # الأوردر من غير customer_id: لازم phone مطابق للموبايل المسجل
        expected_phone = orders.normalize_phone(ord_data.get("phone") or "")
        input_phone = orders.normalize_phone(phone) if phone else None
        if not input_phone and channel == "whatsapp":
            input_phone = orders.normalize_phone(customer_id)

        if not input_phone:
            return {"status": "error", "reason": "يرجى كتابة رقم الموبايل المسجل به الأوردر لتأكيد هويتك وإتمام الإلغاء"}

        if expected_phone and input_phone != expected_phone:
            return {"status": "error", "reason": "رقم الموبايل غير مطابق لبيانات الأوردر المسجلة"}

    return orders.cancel_order(tenant_id, order_number, reason=reason)


def update_order_address_or_phone_handler(
    tenant_id: str, channel: str, customer_id: str, order_number: int, address: str = "", phone: str = "", current_phone: str = ""
) -> dict:
    """تعديل عنوان الشحن أو رقم الموبايل لأوردر قائم بعد التحقق من الملكية ومطابقة البيانات (Fail-Closed)."""
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

    # 2. التحقق من تطابق هوية العميل (Fail-closed)
    if ord_data.get("customer_id"):
        if not customer_uuid or ord_data["customer_id"] != customer_uuid:
            return {"status": "error", "reason": "عذراً، الأوردر ده مش مسجل بنفس هذا الحساب ولا يمكن تعديله"}
    else:
        # الأوردر من غير customer_id: لازم current_phone مطابق للموبايل المسجل
        norm_current = orders.normalize_phone(current_phone) if current_phone else None
        if not norm_current and channel == "whatsapp":
            norm_current = orders.normalize_phone(customer_id)
        expected_phone = orders.normalize_phone(ord_data.get("phone") or "")
        if not norm_current:
            return {"status": "error", "reason": "يرجى كتابة رقم الموبايل الحالي المسجل به الأوردر لتأكيد هويتك وإتمام التعديل"}
        if expected_phone and norm_current != expected_phone:
            return {"status": "error", "reason": "رقم الموبايل الحالي غير مطابق لبيانات الأوردر المسجلة"}

    target_addr = (address or "").strip()
    target_phone = (phone or "").strip()

    if not target_addr and not target_phone:
        return {"status": "error", "reason": "لم يتم تحديد عنوان أو رقم موبايل جديد لتعديله"}

    return orders.update_order_shipping(tenant_id, order_number, phone=target_phone, address=target_addr)


DEFAULT_INSPECTION_POLICY = "متاح المعاينة والقياس مع مندوب الشحن قبل الاستلام والدفع."

PICKUP_ONLY_BLOCK = """

⚠️ تعليمات خاصة وإلزامية لهذا المحل (بتلغي أي قاعدة قبلها بتقول إن الشحن متاح):
هذا المحل **مفيهوش خدمة شحن أو توصيل نهائياً**. الاستلام من المحل بس والدفع عند الاستلام في المحل.
- ممنوع تقول أو تلمّح إن في شحن أو توصيل لباب البيت أو مندوب شحن. بدل "جاهز للشحن" قول "جاهز للاستلام من المحل".
- لو العميل سأل عن الشحن أو التوصيل أو مصاريفه: اعتذر بلطف إن الاستلام من المحل بس، ونادِ get_branches وقوله العنوان ومواعيد العمل ورابط اللوكيشن.
- خطوات الأوردر: اجمع الاسم ورقم الموبايل بس (ماتسألش عن المحافظة ولا عنوان التوصيل). لو في أكتر من فرع اسأله هيستلم من أنهي فرع.
- نادِ quote_order بالمحافظة = "استلام من المحل"، ونادِ place_order بنفس القيمة في المحافظة، وفي العنوان اكتب اسم الفرع اللي هيستلم منه (أو "استلام من المحل").
- ملخص الأوردر: الأصناف والكميات والإجمالي (بدون شحن)، والدفع عند الاستلام في المحل، وإنه يقدر يعاين ويقيس قبل الدفع.
- بعد تسجيل الأوردر قوله إن الأوردر اتحجزله والمحل هيتواصل معاه لتأكيد ميعاد الاستلام.
"""


def get_tenant_settings(tenant_id: str) -> dict:
    """جلب سياسات وإعدادات المحل (مواعيد العمل، المعاينة، الاسترجاع) من جدول tenant_settings"""
    defaults = {
        "working_hours": "يومياً من الساعة 10:00 صباحاً حتى 11:00 مساءً ما عدا الجمعة من 2:00 ظهراً.",
        "inspection_policy": DEFAULT_INSPECTION_POLICY,
        "return_policy": "الاستبدال والاسترجاع متاح خلال 14 يوماً من تاريخ الاستلام بشرط سلامة المنتج ووجود الفاتورة.",
    }
    if not tenant_id:
        return defaults

    try:
        res = supabase.table("tenant_settings").select("*").eq("tenant_id", tenant_id).execute()
        if res.data:
            row = res.data[0]
            return {
                "working_hours": row.get("working_hours") or row.get("hours") or defaults["working_hours"],
                "inspection_policy": row.get("inspection_policy") or row.get("inspection") or defaults["inspection_policy"],
                "return_policy": row.get("return_policy") or row.get("return_exchange") or defaults["return_policy"],
            }
    except Exception:
        pass
    return defaults


def build_system_instruction(tenant_id: str, allow_orders: bool = True) -> str:
    """بناء التعليمات الإرشادية للنظام مع تضمين سياسات المحل من tenant_settings بدلاً من النص الثابت"""
    settings = get_tenant_settings(tenant_id)
    hours = settings["working_hours"]
    inspection = settings["inspection_policy"]
    return_policy = settings["return_policy"]

    shipping_on = orders.is_shipping_enabled(tenant_id)
    if not shipping_on and inspection == DEFAULT_INSPECTION_POLICY:
        inspection = "متاح المعاينة والقياس في المحل قبل الدفع."

    policies_block = f"""
قواعد وسياسات ومواعيد المحل المعتمدة (يجب الالتزام بها نصاً في جميع إجاباتك):
- مواعيد وساعات العمل: {hours}
- سياسة المعاينة قبل الاستلام: {inspection}
- سياسة الاستبدال والاسترجاع: {return_policy}
"""
    if not shipping_on:
        policies_block += PICKUP_ONLY_BLOCK

    if allow_orders:
        return SYSTEM_PROMPT + "\n\n" + policies_block
    else:
        comments_warning = (
            "\n\nتنبيه هام للتعليقات العامة (Comments): أنت ترد في التعليقات العامة فقط. "
            "أدوات تسجيل أو تعديل أو إلغاء الأوردرات غير مفعلة هنا لحماية خصوصية بيانات العميل. "
            "إذا رغب العميل في الشراء أو الطلب، أجب على استفساره بلطف وشجعه على إرسال رسالة خاصة (DM) لتسجيل طلبه فوراً."
        )
        return SYSTEM_PROMPT + "\n\n" + policies_block + comments_warning


def _rule_based_classify_comment(text: str, post_text: str = "") -> str:
    """محرك القواعد الذكي لتصنيف التعليقات كاحتياطي موثوق"""
    norm = _norm_ar((text or "").lower())

    # 1. شكوى
    if any(k in norm for k in ["اتاخر", "اتأخر", "تاخير", "تأخير", "محدش بيرد", "نصابين", "وحش", "سيء", "شكوى", "مشكلة", "باظ", "مكسور", "عيب", "زفت"]):
        return "complaint"

    # 2. طلب شراء (order له أولوية على العروض)
    if any(k in norm for k in ["عايز اطلب", "عايز أطلب", "احجزلي", "احجز لي", "طلب شراء", "سجلي", "سجل لي", "ابعتلي اوردر"]):
        return "order"

    # 3. مدح وشكر
    if any(k in norm for k in ["تحفة", "تحفه", "روعة", "روعه", "جميل", "جميله", "تسلم", "تسلموا", "عاش", "ما شاء الله", "ممتاز", "خامة ممتازة", "ذوق", "شكرا", "شكراً"]):
        return "praise"

    # 4. مكان وعنوان
    if any(k in norm for k in ["فين المكان", "العنوان", "عنوانكم", "المكان فين", "اللوكيشن", "مكانكم", "فين المحل", "عنوان المحل", "فروعكم", "ليكم فرع", "فين فرع", "موقعكم", "فينكم"]):
        return "location"

    # 5. مواعيد العمل
    if any(k in norm for k in ["فاتحين", "مواعيد", "لحد كام", "ساعة كام", "الساعة كام", "اجازة", "إجازة", "شغالين"]):
        return "hours"

    # 6. عروض وخصومات
    if any(k in norm for k in ["القطعتين", "قطعتين", "عرض", "عروض", "خصم", "تخفيض", "لو خدت 2", "سعر القطعتين"]):
        return "offer"

    # 7. شحن وتوصيل
    if any(k in norm for k in ["شحن", "توصيل", "يوصل", "مصاريف الشحن", "بتشحنوا"]):
        return "shipping"

    # 8. أسعار
    if any(k in norm for k in ["بكام", "السعر", "سعره", "سعرها", "بكم", "تكلفة", "السعر كام"]):
        return "price"

    # 9. توفر ومقاسات
    if any(k in norm for k in ["متاح", "متوفر", "المقاس", "مقاس", "اللون", "الوان", "خلصان", "لسه في"]):
        return "availability"

    if len(text.strip()) < 3 or text.strip().startswith("@"):
        return "skip"

    return "skip"


def classify_comment(text: str, post_text: str = "") -> str:
    """فرز وتصنيف تعليقات العملاء على منشورات المتاجر باستخدام Gemini إلى واحد من 10 تصنيفات معتمدة"""
    clean_text = (text or "").strip()
    if not clean_text:
        return "skip"

    VALID_CATEGORIES = {
        "location", "hours", "price", "availability",
        "shipping", "offer", "order", "praise", "complaint", "skip"
    }

    # فحص سريع لتعليقات المنشن أو الإيموجي أو النقط
    if clean_text in [".", "..", "...", "نقطة"]:
        return "price"
    if clean_text in ["up", "متابع", "مهتم"]:
        return "skip"

    prompt = f"""أنت نظام ذكي لفرز وتصنيف تعليقات السوشيال ميديا لمتاجر التجارة الإلكترونية.
صنف التعليق التالي بناءً على نصه ومحتوى البوست المرتبط به إلى واحد فقط من التصنيفات التالية بدقة:
- location: سؤال عن العنوان، المكان، اللوكيشن، أماكن الفروع، في محافظة إيه، فين المحل (مثال: 'فين المكان؟'، 'العنوان').
- hours: سؤال عن مواعيد العمل، فاتحين لغاية امتى، أوقات الفتح والإغلاق، الإجازات.
- price: سؤال عن السعر، التكلفة، ده بكام (مثال: 'ده بكام؟'، 'السعر كام؟').
- availability: سؤال عن توفر منتج أو مقاسات أو ألوان معينة (مثال: 'المقاس متاح؟'، 'في منه أسود؟').
- shipping: سؤال عن الشحن، التوصيل، مصاريف الشحن، مدة التوصيل، الشحن للمحافظات (مثال: 'في شحن؟'، 'الشحن بكام لأسيوط؟').
- offer: سؤال عن العروض، الخصومات، التخفيضات، شراء أكثر من قطعة، سيل، عرض (مثال: 'القطعتين ولا واحدة؟'، 'في عرض؟').
- order: طلب شراء صريح، حجز، رغبة في عمل أوردر، إرسال بيانات (مثال: 'عايز أطلب تيشيرت أسود'، 'احجزلي').
- praise: شكر، إشادة، إعجاب بالمنتج أو الخدمة، كلام جميل (مثال: 'حاجتكم تحفة تسلموا'، 'الخامة روعة').
- complaint: شكوى، غضب، مشكلة في أوردر، تأخير شحن، عيب صناعة، معاملة سيئة (مثال: 'الأوردر اتأخر ومحدش بيرد').
- skip: منشن لأشخاص، إيموجي مجرد، تعليقات عشوائية، أو كلام غير مفهوم لا يحتاج رد.

محتوى البوست المرتبط:
{post_text or 'غير متوفر'}

تعليق العميل:
{clean_text}

أجب بصيغة JSON فقط:
{{"category": "<one of: location, hours, price, availability, shipping, offer, order, praise, complaint, skip>"}}
"""

    try:
        config = types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        )
        res = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=config,
        )
        raw_json = (res.text or "").strip()
        data = json.loads(raw_json)
        cat = str(data.get("category", "")).strip().lower()
        if cat in VALID_CATEGORIES:
            return cat
    except Exception as e:
        pass

    return _rule_based_classify_comment(clean_text, post_text)


# ---------------------------------------------------------------
# 🖼️ صور المنتجات للعملاء: الأداة بتحط الروابط في طابور، والسيرفر بيبعتها كصور فعلية بعد الرد النصي
# ---------------------------------------------------------------
PENDING_IMAGES = {}  # session_key -> [image urls]
PENDING_IMAGES_LOCK = threading.Lock()
MAX_IMAGES_PER_REPLY = 3
MAX_PENDING_IMAGE_SESSIONS = 1000


def pop_customer_images(tenant_id: str, channel: str, customer_id: str, allow_orders: bool = True) -> list:
    """بيرجّع (ويفضّي) صور المنتجات اللي البوت قرر يبعتها للعميل في آخر رد."""
    mode_tag = "orders" if allow_orders else "comments"
    key = f"{tenant_id}:{channel}:{customer_id}:{mode_tag}"
    with PENDING_IMAGES_LOCK:
        return PENDING_IMAGES.pop(key, [])


def handle_customer_message(
    tenant_id: str,
    customer_id: str,
    text: str = "",
    image_bytes: bytes = None,
    image_mime_type: str = "image/jpeg",
    channel: str = "messenger",
    allow_orders: bool = True,
    is_comment: bool = False,
) -> str:
    """معالجة رسالة عميل (نصية أو صورة) جاية من أي منصة وحفظ سياق الشات وتطبيق السياسات"""

    # فحص حد المعدل: سقف منفصل للتعليقات عن المحادثات
    if is_comment or not allow_orders or channel in ["facebook_comment", "instagram_comment", "tiktok_comment"]:
        is_allowed, rate_msg = RATE_LIMITER.check_comment_rate_limit(tenant_id, customer_id)
    else:
        is_allowed, rate_msg = RATE_LIMITER.check_rate_limit(tenant_id, customer_id)

    if not is_allowed:
        return rate_msg

    mode_tag = "orders" if allow_orders else "comments"
    session_key = f"{tenant_id}:{channel}:{customer_id}:{mode_tag}"
    now = time.time()
    with PENDING_IMAGES_LOCK:
        PENDING_IMAGES.pop(session_key, None)  # امسح أي صور قديمة ماتبعتتش

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
        return db_check_stock(tenant_id, product_name, size=size or None, color=color or None, reveal_qty=False)

    def quote_order(items: list[str], governorate: str, promo_code: str = "") -> dict:
        """حساب سعر الأوردر والشحن والإجمالي وتطبيق كود الخصم وعروض الشحن المجاني من غير ما تسجل أوردر. يجب استدعاؤها قبل عرض الملخص على العميل.
        
        Args:
            items: قائمة الأصناف بصيغة إجبارية دقيقة: ['SKU:الكمية'] (مثال: ['TS-BLK-L:1'] أو ['TS-BLK-L:2', 'HD-GRY-XL:1']) حيث كود الصنف (sku) يأتي من أداة check_stock.
            governorate: اسم المحافظة بالعربي لحساب مصاريف الشحن (مثال: 'القاهرة', 'الجيزة', 'الإسكندرية').
            promo_code: كود الخصم إذا ذكره العميل للاستفادة من التخفيض (اختياري).
            
        Returns:
            dict يحتوي على تفاصيل الأسعار، الخصم، الشحن، والإجمالي النهائي وطريقة الدفع.
        """
        if not orders.is_shipping_enabled(tenant_id):
            governorate = orders.PICKUP_LABEL
        return orders.quote_order(tenant_id, items, governorate, promo_code=promo_code, session_id=session_key)

    def send_product_image(sku: str) -> dict:
        """بتجهّز صورة المنتج علشان تتبعت للعميل كصورة فعلية في الشات بعد ردك مباشرة.
        نادِها لما العميل يطلب يشوف صورة المنتج أو اللون. لازم الـ sku يكون جاي من check_stock.

        Args:
            sku: كود الصنف (sku) اللي رجع من أداة check_stock
        """
        try:
            rows = (
                supabase.table("variants")
                .select("image_url,products!inner(image_url)")
                .eq("tenant_id", tenant_id)
                .eq("sku", sku.strip())
                .execute()
                .data
            )
        except Exception as e:
            print("فشل جلب صورة الصنف:", type(e).__name__)
            return {"status": "error", "reason": "حصلت مشكلة مؤقتة في جلب الصورة"}
        if not rows:
            return {"status": "error", "reason": "كود الصنف مش موجود"}
        url = rows[0].get("image_url") or (rows[0].get("products") or {}).get("image_url")
        if not url or not str(url).startswith("https://"):
            return {"status": "no_image", "message": "مفيش صورة متسجلة للصنف ده حالياً، اعتذر للعميل بلطف."}
        with PENDING_IMAGES_LOCK:
            if len(PENDING_IMAGES) > MAX_PENDING_IMAGE_SESSIONS:
                PENDING_IMAGES.clear()
            queue = PENDING_IMAGES.setdefault(session_key, [])
            if url in queue:
                return {"status": "queued", "message": "الصورة جاهزة وهتتبعت بعد ردك."}
            if len(queue) >= MAX_IMAGES_PER_REPLY:
                return {"status": "limit", "message": f"أقصى عدد صور في الرد الواحد {MAX_IMAGES_PER_REPLY}."}
            queue.append(str(url))
        return {"status": "queued", "message": "الصورة هتتبعت للعميل تلقائياً بعد ردك. ماتكتبش الرابط في الرد."}

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
        if not orders.is_shipping_enabled(tenant_id):
            governorate = orders.PICKUP_LABEL
        if not orders.has_valid_quote(session_key, items, governorate):
            return {
                "status": "error",
                "reason": "يجب حساب تكلفة الأوردر بـ quote_order أولاً في نفس الجلسة بنفس الأصناف والمحافظة قبل تأكيد الطلب"
            }
        return orders.place_order(
            tenant_id, customer_name, phone, governorate, address, items,
            channel=channel, channel_user_id=customer_id, promo_code=promo_code,
            session_id=session_key
        )

    def get_branches(governorate: str = "") -> dict:
        """بترجّع فروع ومحلات البراند، العناوين، مواعيد العمل، ورابط اللوكيشن على الخريطة.
        
        Args:
            governorate: اسم المحافظة أو المنطقة اللي العميل بيسأل عنها (سيبها فاضية لو بيسأل عن كل الفروع)
        """
        query = supabase.table("branches").select("name, governorate, city, address, google_maps_url, working_hours, phone").eq("tenant_id", tenant_id)
        if governorate:
            query = query.ilike("governorate", f"%{escape_like(governorate.strip())}%")
        rows = query.execute().data
        if not rows:
            if not orders.is_shipping_enabled(tenant_id):
                return {
                    "status": "no_branches_pickup_only",
                    "message": "المحل ده بدون شحن والاستلام من المحل بس، ومفيش عنوان مسجل لسه. اعتذر للعميل وقوله إن المحل هيتواصل معاه بالعنوان.",
                }
            # لو مفيش فروع مسجلة، نرجع إنه متجر أونلاين بشحن فوري
            return {
                "status": "online_only",
                "message": "إحنا متجر أونلاين وشغالين شحن لجميع محافظات مصر لحد باب البيت، وتقدر تعاين الشحنة قبل الاستلام!"
            }
        return {"status": "ok", "branches": rows}

    def get_shipping_rates(governorate: str = "") -> dict:
        """معرفة مصاريف وأسعار الشحن ومناطق التوصيل المتاحة لمختلف المحافظات.
        
        Args:
            governorate: اسم المحافظة بالعربي (مثال: القاهرة، الجيزة، الإسكندرية، أسيوط). سيبها فاضية لو عايز تعرف كل أسعار الشحن.
            
        Returns:
            dict يحتوي على أسعار الشحن المتاحة أو سعر شحن المحافظة المطلوبة.
        """
        if not orders.is_shipping_enabled(tenant_id):
            return {"status": "no_shipping", "message": "المحل ده مفيهوش شحن، الاستلام من المحل بس. اعتذر للعميل بلطف ووجّهه للفروع."}
        try:
            res = supabase.table("shipping_rates").select("governorate,price").eq("tenant_id", tenant_id).execute()
            rows = res.data or []
            if not rows:
                return {
                    "status": "default",
                    "rates": [{"governorate": "جميع المحافظات", "price": 50}],
                    "message": "مصاريف الشحن 50 جنيه لجميع المحافظات والتوصيل خلال 2-4 أيام عمل مع إمكانية المعاينة قبل الاستلام."
                }

            if governorate and governorate.strip():
                wanted = _norm_ar(governorate)
                for r in rows:
                    gov_norm = _norm_ar(r.get("governorate", ""))
                    if gov_norm == wanted or wanted in gov_norm or gov_norm in wanted:
                        return {
                            "status": "ok",
                            "governorate": r["governorate"],
                            "price": r["price"],
                            "message": f"مصاريف الشحن لمحافظة {r['governorate']} هي {r['price']} جنيه والتوصيل خلال 2-4 أيام عمل مع إمكانية المعاينة قبل الاستلام."
                        }
                # فحص الشحن الموحد إذا لم تكن المحافظة مسجلة بشكل مخصص
                for r in rows:
                    gov_norm = _norm_ar(r.get("governorate", ""))
                    if gov_norm in ("جميع المحافظات", "شحن موحد", "كل المحافظات", "الكل", "عام"):
                        return {
                            "status": "ok",
                            "governorate": governorate,
                            "price": r["price"],
                            "message": f"مصاريف الشحن {r['price']} جنيه لجميع المحافظات والتوصيل خلال 2-4 أيام عمل مع إمكانية المعاينة قبل الاستلام."
                        }

            # إذا سأل العميل بشكل عام وكان هناك شحن موحد
            for r in rows:
                gov_norm = _norm_ar(r.get("governorate", ""))
                if gov_norm in ("جميع المحافظات", "شحن موحد", "كل المحافظات", "الكل", "عام"):
                    return {
                        "status": "ok",
                        "rates": rows,
                        "price": r["price"],
                        "message": f"مصاريف الشحن {r['price']} جنيه لجميع المحافظات والتوصيل خلال 2-4 أيام عمل مع إمكانية المعاينة قبل الاستلام."
                    }

            return {
                "status": "ok",
                "rates": rows,
                "message": "قائمة أسعار الشحن المتاحة للمحافظات."
            }
        except Exception as e:
            return {
                "status": "fallback",
                "price": 50,
                "message": "مصاريف الشحن 50 جنيه لجميع المحافظات والتوصيل سريع لباب البيت مع المعاينة."
            }

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
            query = query.ilike("name", f"%{escape_like(branch_name.strip())}%")
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
        return cancel_customer_order_handler(tenant_id, channel, customer_id, order_number, phone=phone, reason=reason)

    def update_order_address_or_phone(order_number: int, address: str = "", phone: str = "", current_phone: str = "") -> dict:
        """تعديل عنوان الشحن أو رقم الموبايل لأوردر قائم بناءً على طلب العميل بعد التأكد من ملكيته للأوردر.
        
        Args:
            order_number: رقم الأوردر المراد تعديله (مثلاً 5)
            address: العنوان الجديد بالتفصيل إن كان العميل يريد تغييره
            phone: رقم الموبايل الجديد إن كان العميل يريد تغييره
            current_phone: رقم الموبايل الحالي المسجل به الأوردر لتأكيد هوية العميل
        """
        return update_order_address_or_phone_handler(
            tenant_id, channel, customer_id, order_number, address=address, phone=phone, current_phone=current_phone
        )

    # 1. تحديد هوية العميل وسجل المحادثة في قاعدة البيانات
    cust_uuid = orders.get_or_create_customer(tenant_id, channel, customer_id)
    conv_id = get_or_create_conversation(tenant_id, cust_uuid, channel) if cust_uuid else None

    # 2. استرجاع سياق الرسائل السابقة من الداتابيز قبل قفل الذاكرة CHATS_DICT_LOCK لمنع حظر بقية الجلسات
    history = load_recent_messages(conv_id, limit=14) if conv_id else []

    # 3. إنشاء أو استرجاع الجلسة تحت قفل قاموس الجلسات مع تنظيف دوري للذاكرة
    ship_on = orders.is_shipping_enabled(tenant_id)
    with CHATS_DICT_LOCK:
        cleanup_customer_chats()
        # لو التاجر غيّر إعداد الشحن، نعيد بناء الجلسة بالتعليمات الجديدة (السجل بيتحمّل من الداتابيز)
        if session_key in CUSTOMER_CHATS and CUSTOMER_CHATS[session_key].get("shipping_enabled", True) != ship_on:
            CUSTOMER_CHATS.pop(session_key, None)
        if session_key not in CUSTOMER_CHATS:
            if allow_orders:
                tools = [
                    check_stock, quote_order, place_order, get_branches,
                    submit_customer_review, get_google_review_link,
                    get_active_offers, cancel_customer_order, update_order_address_or_phone,
                    send_product_image
                ]
                if ship_on:
                    tools.append(get_shipping_rates)
            else:
                # مسار التعليقات: عدم تمرير أدوات الأوردر والتعديل والإلغاء وتوجيه العميل للخاص
                tools = [
                    check_stock, get_branches,
                    submit_customer_review, get_google_review_link, get_active_offers
                ]
                if ship_on:
                    tools.append(get_shipping_rates)
            sys_inst = build_system_instruction(tenant_id, allow_orders=allow_orders)

            config = types.GenerateContentConfig(
                system_instruction=sys_inst,
                tools=tools,
            )
            chat = client.chats.create(model=MODEL, config=config, history=history or None)
            CUSTOMER_CHATS[session_key] = {
                "chat": chat,
                "last_active": now,
                "conversation_id": conv_id,
                "lock": threading.Lock(),
                "shipping_enabled": ship_on,
            }
        else:
            CUSTOMER_CHATS[session_key]["last_active"] = now

        session_entry = CUSTOMER_CHATS[session_key]
        chat = session_entry["chat"]
        session_lock = session_entry["lock"]

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

    # قفل threading.Lock لكل جلسة لمنع تداخل الرسائل لنفس العميل
    with session_lock:
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
