import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import secrets
import string
import time
import httpx
from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, Request, Response, Query, BackgroundTasks, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import urllib.parse

from check_stock import supabase
from image_utils import optimize_product_image, upload_product_image_to_storage
from customer_agent import handle_customer_message, RATE_LIMITER, classify_comment, pop_customer_images, purge_old_chat_messages
import orders
from orders import is_shipping_enabled
from merchant_settings import (
    load_merchant_settings,
    save_merchant_settings,
    is_channel_allowed,
    check_and_increment_whatsapp_usage,
)
from notify import send_complaint_notification, submit_praise_review

import threading
from collections import OrderedDict, deque

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()


class MessageDeduplicator:
    """منع تكرار معالجة الرسائل والتعليقات عند إعادة إرسالها من Meta أو الشبكة"""
    def __init__(self, max_size=10000, ttl_seconds=900):  # 15 دقيقة صلاحية منع التكرار
        self.seen = OrderedDict()  # id -> timestamp
        self.max_size = max_size
        self.ttl = ttl_seconds

    def is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        now = time.time()
        if msg_id in self.seen:
            ts = self.seen[msg_id]
            if now - ts < self.ttl:
                return True
            del self.seen[msg_id]

        while len(self.seen) >= self.max_size:
            self.seen.popitem(last=False)

        self.seen[msg_id] = now
        return False


DEDUPLICATOR = MessageDeduplicator()


class TikTokRateLimiter:
    """
    التحكم الصارم في معدل طلبات TikTok Open API v2
    الحد الأقصى الرسمي لتيك توك هو 600 طلب/دقيقة.
    نضع حداً آمناً عند 550 طلب/دقيقة (نافذة منزلقة 60 ثانية) مع دعم Backoff عند ورود 429
    لمنع حظر الحسابات أو رفض الطلبات.
    """
    def __init__(self, max_requests: int = 550, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.timestamps = deque()
        self.lock = asyncio.Lock()
        self.blocked_until = 0.0

    async def acquire(self):
        async with self.lock:
            while True:
                now = time.time()
                # في حال تفعيل فترة الانتظار بعد خطأ 429
                if now < self.blocked_until:
                    sleep_time = self.blocked_until - now + 0.1
                    await asyncio.sleep(sleep_time)
                    continue

                # تصفية الطلبات الأقدم من 60 ثانية من النافذة المنزلقة
                while self.timestamps and (now - self.timestamps[0]) >= self.window_seconds:
                    self.timestamps.popleft()

                # إذا كان عدد الطلبات أقل من السقف الآمن
                if len(self.timestamps) < self.max_requests:
                    self.timestamps.append(now)
                    return

                # إذا بلغنا السقف، ننتظر حتى يسقط أقدم طلب من النافذة
                oldest = self.timestamps[0]
                wait_time = self.window_seconds - (now - oldest) + 0.05
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

    def record_rate_limited(self, retry_after: float = 60.0):
        """تسجيل ورود خطأ 429 من تيك توك لتأجيل كافة الطلبات القادمة بأمان"""
        self.blocked_until = time.time() + retry_after
        print(f"⚠️ [TikTok Rate Limit] تم تفعيل وضع الانتظار لتيك توك لمدة {retry_after} ثانية لتفادي الرفض.")


TIKTOK_RATE_LIMITER = TikTokRateLimiter()


def normalize_comment_intent(text: str) -> Optional[str]:
    """
    تحليل نية الكومنت السريع المتكرر (مثل: تفاصيل، العنوان فين، بكام، تم)
    لإمكانية إرجاع رد مخزن مسبقاً (كاش) وتوفير آلاف التوكنز.
    """
    if not text:
        return None
    # إزالة التشكيل وعلامات الترقيم والرموز التعبيرية
    cleaned = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    if not cleaned:
        return None

    # توحيد الحروف العربية
    cleaned = cleaned.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")

    # 1. تفاصيل (Details)
    details_exact = {
        "تفاصيل", "تفاصيل اكتر", "ممكن تفاصيل", "تفاصيل لو سمحت", "عايز تفاصيل",
        "ابعتلي تفاصيل", "ممكن التفاصيل", "التفاصيل", "شرح", "تفاصيل ايه",
        "تفاصيل اكتر لو سمحت", "عاوز تفاصيل", "تفاصيل لاهنت", "تفاصيل كدا",
        "ممكن اعرف تفاصيل", "ممكن تفاصيل اكتر", "تفاصيل كامله",
    }
    if cleaned in details_exact or cleaned.startswith("تفاصيل ") or cleaned.endswith(" تفاصيل"):
        return "INTENT_DETAILS"

    # 2. العنوان والمكان (Location)
    location_exact = {
        "العنوان فين", "فين العنوان", "المكان فين", "فين المكان", "مكانكم فين",
        "العنوان", "المكان", "فين", "عنوانكم", "عنوانكم فين", "اقرب فرع",
        "المحل فين", "فين فرعكم", "موقعكم", "موقعكم فين", "المحل وين",
        "فين في القاهره", "فرعكم فين", "عنوان المحل", "مكان المحل",
    }
    if cleaned in location_exact:
        return "INTENT_LOCATION"

    # 3. السعر (Price)
    price_exact = {
        "بكام", "السعر كام", "سعره كام", "كام السعر", "بكام ده", "السعر",
        "سعر", "السعر كام لو سمحت", "سعره كام لو سمحت", "بكام الشحن", "سعر كام",
        "بكام القطعه", "بكام التيشيرت", "سعره", "اسعاركم كام",
    }
    if cleaned in price_exact or cleaned.startswith("بكام ") or cleaned.startswith("سعره "):
        return "INTENT_PRICE"

    # 4. تأكيد أو تم (Confirm / Done)
    confirm_exact = {"تم", "تم تمام", "done", "تمام", "اوكي", "ماشي", "تسلم", "شكرا"}
    if cleaned in confirm_exact:
        return "INTENT_CONFIRM"

    # 5. تعليقات نصية قصيرة مكررة (أقل أو يساوي 3 كلمات)
    words = cleaned.split()
    if len(words) <= 3:
        return f"TEXT_{'_'.join(words)}"

    return None


class CommentResponseCache:
    """
    تخزين مؤقت (كاش) لردود الكومنتات المتكررة لمدة 5 دقائق (300 ثانية).
    يستبدل اسم العميل في القالب ديناميكياً لتظهر الإجابة شخصية وودودة
    مع توفير 80% إلى 95% من استهلاك التوكنز في البوستات والريلز الفيروسية.
    """
    def __init__(self, ttl_seconds: int = 300, max_entries: int = 2000):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.cache = {}  # key -> {"template": str, "private_template": Optional[str], "expires_at": float}
        self.lock = threading.Lock()

    def _cleanup_expired(self, now: float):
        expired = [k for k, v in self.cache.items() if now >= v["expires_at"]]
        for k in expired:
            del self.cache[k]
        while len(self.cache) >= self.max_entries:
            oldest_key = min(self.cache.keys(), key=lambda k: self.cache[k]["expires_at"])
            del self.cache[oldest_key]

    def get(self, tenant_id: str, post_id: str, intent_key: str, sender_name: str = "يا فندم") -> Optional[tuple[str, Optional[str]]]:
        if not intent_key:
            return None
        key = f"{tenant_id}:{post_id or 'global'}:{intent_key}"
        now = time.time()
        with self.lock:
            self._cleanup_expired(now)
            item = self.cache.get(key)
            if not item:
                fallback_key = f"{tenant_id}:global:{intent_key}"
                item = self.cache.get(fallback_key)

            if item and now < item["expires_at"]:
                pub_tpl = item["template"]
                priv_tpl = item.get("private_template")
                
                # إدراج اسم العميل بشكل مخصص
                if "{name}" in pub_tpl:
                    pub_reply = pub_tpl.format(name=sender_name)
                else:
                    pub_reply = f"أهلاً {sender_name}! {pub_tpl}"

                priv_reply = None
                if priv_tpl:
                    if "{name}" in priv_tpl:
                        priv_reply = priv_tpl.format(name=sender_name)
                    else:
                        priv_reply = f"أهلاً {sender_name} 🙏\n{priv_tpl}"

                return (pub_reply, priv_reply)
        return None

    def set(self, tenant_id: str, post_id: str, intent_key: str, template: str, private_template: Optional[str] = None):
        if not intent_key or not template:
            return
        key = f"{tenant_id}:{post_id or 'global'}:{intent_key}"
        now = time.time()
        with self.lock:
            self._cleanup_expired(now)
            self.cache[key] = {
                "template": template,
                "private_template": private_template,
                "expires_at": now + self.ttl,
            }

    def clear(self):
        with self.lock:
            self.cache.clear()


COMMENT_CACHE = CommentResponseCache(ttl_seconds=300)

# توكن التحقق السري ومفاتيح التوقيع الخاصة بالـ Webhooks
ALLOW_UNSIGNED = os.environ.get("ALLOW_UNSIGNED_WEBHOOKS") == "1"  # للتطوير المحلي بس
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
TIKTOK_APP_SECRET = os.environ.get("TIKTOK_APP_SECRET", "")


def check_startup_warnings():
    """فحص إعدادات الأمان عند الإقلاع وطباعة تحذيرات واضحة"""
    if not META_APP_SECRET:
        print("⚠️ [تحذير أمني] META_APP_SECRET غير مضبوط في متغيرات البيئة! سيتم رفض توقيعات الـ Webhooks ما لم يتم تفعيل وضع التطوير.")
    if not META_VERIFY_TOKEN:
        print("⚠️ [تحذير] META_VERIFY_TOKEN غير مضبوط في متغيرات البيئة! لن تتمكن خوادم Meta من التحقق من رابط الـ Webhook.")
    if ALLOW_UNSIGNED:
        print("🚨 [تحذير أمني خطير] وضع ALLOW_UNSIGNED_WEBHOOKS=1 مفعّل! السيرفر سيقبل طلبات Webhook غير موثقة (مخصص فقط للتطوير المحلي، خطر جداً في الإنتاج).")


async def periodic_chat_cleanup():
    """مهمة خلفية دورية لحذف شاتات العملاء القديمة التي مضى عليها أكثر من 20 يوماً لتوفير مساحة الداتابيز"""
    while True:
        try:
            await asyncio.to_thread(purge_old_chat_messages, 20)
        except Exception as e:
            print("❌ خطأ في المهمة المجدولة لتنظيف الشات:", e)
        # تشغيل مرة كل 24 ساعة (86400 ثانية)
        await asyncio.sleep(86400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_startup_warnings()
    cleanup_task = asyncio.create_task(periodic_chat_cleanup())
    yield
    cleanup_task.cancel()


app = FastAPI(title="AI Agency Omnichannel Webhook Server", lifespan=lifespan)


def verify_meta_signature(raw_body: bytes, signature_header: str) -> bool:
    """التحقق من توقيع X-Hub-Signature-256 لمنع استهلاك الذكاء الاصطناعي بطلبات مزيفة"""
    if not META_APP_SECRET:
        return ALLOW_UNSIGNED
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected_hash = signature_header.split("sha256=")[1].strip()
    computed_hash = hmac.new(
        key=META_APP_SECRET.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed_hash, expected_hash)


def verify_tiktok_signature(raw_body: bytes, signature_header: str) -> bool:
    """التحقق من توقيع تيك توك باستخدام TIKTOK_APP_SECRET"""
    if not TIKTOK_APP_SECRET:
        return ALLOW_UNSIGNED
    if not signature_header:
        return False
    clean_sig = signature_header.replace("sha256=", "").strip()
    computed_hash = hmac.new(
        key=TIKTOK_APP_SECRET.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed_hash, clean_sig)


@app.get("/")
def home():
    return {"status": "running", "service": "AI Agency Omnichannel Webhook"}


# ==========================================
# 1. Meta Webhook (Facebook Messenger, Instagram, WhatsApp)
# ==========================================

@app.get("/webhook/meta")
def verify_meta_webhook(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    """التحقق التلقائي المطلوب من شركة Meta عند ربط الـ Webhook"""
    if META_VERIFY_TOKEN and mode == "subscribe" and token and hmac.compare_digest(token, META_VERIFY_TOKEN):
        print("✅ تم التحقق من Meta Webhook بنجاح!")
        return Response(content=challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)


async def send_meta_reply(recipient_id: str, text: str, page_access_token: str, platform: str = "messenger", comment_id: str = None):
    """إرسال رد خاص (DM أو Private Reply) للعميل على ماسنجر أو إنستجرام"""
    url = "https://graph.facebook.com/v21.0/me/messages"
    headers = {
        "Authorization": f"Bearer {page_access_token}",
        "Content-Type": "application/json",
    }
    recipient = {"comment_id": comment_id} if comment_id else {"id": recipient_id}
    payload = {
        "recipient": recipient,
        "message": {"text": text}
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"❌ خطأ في إرسال الرد لـ {platform}:", r.text)
        except Exception as e:
            print(f"❌ استثناء في إرسال {platform}:", type(e).__name__)


async def send_meta_image(recipient_id: str, image_url: str, page_access_token: str, platform: str = "messenger"):
    """إرسال صورة فعلية للعميل في الشات الخاص على ماسنجر أو إنستجرام (رابط عام https)"""
    url = "https://graph.facebook.com/v21.0/me/messages"
    headers = {"Authorization": f"Bearer {page_access_token}", "Content-Type": "application/json"}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"attachment": {"type": "image", "payload": {"url": image_url, "is_reusable": True}}},
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code != 200:
                print(f"❌ خطأ في إرسال صورة لـ {platform}:", r.text[:200])
        except Exception as e:
            print(f"❌ استثناء في إرسال صورة {platform}:", type(e).__name__)


async def like_meta_comment(comment_id: str, page_access_token: str):
    """عمل إعجاب (Like) لكومنت العميل لزيادة التفاعل والخوارزميات على فيسبوك/إنستجرام"""
    if not comment_id or not page_access_token:
        return
    url = f"https://graph.facebook.com/v21.0/{comment_id}/likes"
    headers = {"Authorization": f"Bearer {page_access_token}"}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, headers=headers, timeout=6)
            if r.status_code == 200:
                print(f"👍 تم الإعجاب بالكومنت {comment_id}")
        except Exception:
            pass


async def send_public_comment_reply(comment_id: str, text: str, page_access_token: str):
    """الرد العام تحت الكومنت مباشرة على فيسبوك وعمل لايك للكومنت"""
    await like_meta_comment(comment_id, page_access_token)
    url = f"https://graph.facebook.com/v21.0/{comment_id}/comments"
    headers = {
        "Authorization": f"Bearer {page_access_token}",
        "Content-Type": "application/json",
    }
    payload = {"message": text}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"❌ خطأ في الرد على الكومنت:", r.text[:200])
            else:
                print(f"✅ تم الرد العام على الكومنت {comment_id}")
        except Exception as e:
            print(f"❌ استثناء في الرد على الكومنت:", type(e).__name__)


async def send_instagram_comment_reply(comment_id: str, text: str, page_access_token: str):
    """الرد على كومنت إنستجرام وعمل لايك له"""
    await like_meta_comment(comment_id, page_access_token)
    url = f"https://graph.facebook.com/v21.0/{comment_id}/replies"
    headers = {
        "Authorization": f"Bearer {page_access_token}",
        "Content-Type": "application/json",
    }
    payload = {"message": text}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"❌ خطأ في الرد على كومنت إنستجرام:", r.text[:200])
            else:
                print(f"✅ تم الرد على كومنت إنستجرام {comment_id}")
        except Exception as e:
            print(f"❌ استثناء إنستجرام:", type(e).__name__)


POST_TEXT_CACHE = {}  # post_id -> (text, timestamp)
PARENT_PAGE_CACHE = {}  # parent_id -> (is_page, timestamp)


def _store_bounded_cache(cache_dict: dict, key: str, value: tuple, max_size: int = 3000):
    if len(cache_dict) >= max_size and key not in cache_dict:
        oldest_key = min(cache_dict.keys(), key=lambda k: cache_dict[k][1])
        del cache_dict[oldest_key]
    cache_dict[key] = value


async def fetch_post_text(post_id: str, page_access_token: str) -> str:
    """جلب نص البوست من Graph API مع كاش ساعة لتسريع الردود وتوفير الطلبات"""
    if not post_id or not page_access_token:
        return ""
    now = time.time()
    if post_id in POST_TEXT_CACHE:
        cached_text, ts = POST_TEXT_CACHE[post_id]
        if now - ts < 3600:
            return cached_text

    url = f"https://graph.facebook.com/v21.0/{post_id}"
    params = {"fields": "message,story,caption", "access_token": page_access_token}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                msg = (data.get("message") or data.get("story") or data.get("caption") or "").strip()
                _store_bounded_cache(POST_TEXT_CACHE, post_id, (msg, now))
                return msg
    except Exception as e:
        print(f"Error fetching post text for {post_id}: {type(e).__name__}")
    return ""


async def fetch_comment_image(value: dict, comment_id: str, page_access_token: str) -> tuple[Optional[bytes], str]:
    """استخراج وتحميل صورة التعليق إذا توفرت في الحمولة أو عبر Graph API"""
    img_url = None
    if isinstance(value, dict):
        attachment = value.get("attachment")
        if isinstance(attachment, dict):
            img_url = attachment.get("media", {}).get("image", {}).get("src") or attachment.get("url")
        if not img_url:
            img_url = value.get("photo") or value.get("attachment_url")

    if not img_url and comment_id and page_access_token:
        url = f"https://graph.facebook.com/v21.0/{comment_id}"
        params = {"fields": "attachment", "access_token": page_access_token}
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    att = resp.json().get("attachment", {})
                    if isinstance(att, dict):
                        img_url = att.get("media", {}).get("image", {}).get("src")
        except Exception:
            pass

    if img_url:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(img_url)
                if resp.status_code == 200:
                    mime = resp.headers.get("content-type", "image/jpeg")
                    return resp.content, mime
        except Exception as e:
            print(f"Error downloading comment image: {type(e).__name__}")

    return None, "image/jpeg"


async def is_parent_comment_from_page(parent_id: str, page_id: str, page_access_token: str) -> bool:
    """التحقق مما إذا كان التعليق الأب صادراً من الصفحة نفسها لمنع التدخل في ردود العملاء على بعض"""
    if not parent_id or not page_access_token:
        return False
    now = time.time()
    if parent_id in PARENT_PAGE_CACHE:
        is_page, ts = PARENT_PAGE_CACHE[parent_id]
        if now - ts < 1800:
            return is_page

    url = f"https://graph.facebook.com/v21.0/{parent_id}"
    params = {"fields": "from", "access_token": page_access_token}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                from_id = data.get("from", {}).get("id") if isinstance(data.get("from"), dict) else None
                is_page = str(from_id).strip() == str(page_id).strip()
                _store_bounded_cache(PARENT_PAGE_CACHE, parent_id, (is_page, now))
                return is_page
    except Exception as e:
        print(f"Error checking parent comment {parent_id}: {type(e).__name__}")
    return False


async def has_page_already_replied(comment_id: str, page_id: str, page_access_token: str) -> bool:
    """فحص ما إذا كانت الصفحة قد قامت بالرد بالفعل على هذا التعليق عبر Graph API"""
    if not comment_id or not page_access_token:
        return False
    url = f"https://graph.facebook.com/v21.0/{comment_id}/comments"
    params = {"fields": "from", "access_token": page_access_token}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                replies = resp.json().get("data", [])
                for r in replies:
                    from_info = r.get("from", {})
                    if isinstance(from_info, dict) and str(from_info.get("id")).strip() == str(page_id).strip():
                        return True
    except Exception as e:
        print(f"Error checking previous replies for {comment_id}: {type(e).__name__}")
    return False


async def has_ig_already_replied(comment_id: str, page_id: str, page_access_token: str) -> bool:
    """فحص ما إذا كانت الصفحة قد ردت بالفعل على كومنت إنستجرام"""
    if not comment_id or not page_access_token:
        return False
    url = f"https://graph.facebook.com/v21.0/{comment_id}/replies"
    params = {"fields": "from", "access_token": page_access_token}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                replies = resp.json().get("data", [])
                for r in replies:
                    from_info = r.get("from", {})
                    if isinstance(from_info, dict) and str(from_info.get("id")).strip() == str(page_id).strip():
                        return True
    except Exception as e:
        print(f"Error checking previous IG replies for {comment_id}: {type(e).__name__}")
    return False


async def send_whatsapp_reply(to_phone: str, text: str, phone_number_id: str, access_token: str):
    """إرسال رد واتساب عبر WhatsApp Cloud API"""
    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text}
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"❌ خطأ في إرسال واتساب:", r.text[:200])
        except Exception as e:
            print(f"❌ استثناء واتساب:", type(e).__name__)


async def send_whatsapp_image(to_phone: str, image_url: str, phone_number_id: str, access_token: str):
    """إرسال صورة فعلية على واتساب عبر WhatsApp Cloud API (رابط عام https)"""
    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "image",
        "image": {"link": image_url},
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code != 200:
                print("❌ خطأ في إرسال صورة واتساب:", r.text[:200])
        except Exception as e:
            print("❌ استثناء صورة واتساب:", type(e).__name__)


async def process_meta_event(data: dict):
    """معالجة الرسائل الواردة من فيسبوك وإنستجرام وواتساب وتحديد التاجر"""
    entry_list = data.get("entry", [])
    for entry in entry_list:
        # أ) رسائل فيسبوك ماسنجر & إنستجرام
        messaging_list = entry.get("messaging", [])
        for event in messaging_list:
            sender_id = str(event.get("sender", {}).get("id") or "").strip()
            recipient_id = str(event.get("recipient", {}).get("id") or "").strip()  # ده الـ page_id أو instagram_id
            message = event.get("message", {})
            text = message.get("text")
            mid = message.get("mid")

            # منع تكرار الرسائل المكررة من Meta (Deduplication)
            if mid and DEDUPLICATOR.is_duplicate(f"meta_msg:{mid}"):
                print(f"⏩ تم تجاهل رسالة مكررة من Meta (mid: {mid})")
                continue

            # تجاهل الرسائل اللي البوت باعتها بنفسه (echo)
            if not text or message.get("is_echo"):
                continue

            # التحقق الصارم من أن recipient_id أرقام فقط لمنع حقن فلاتر PostgREST
            if not recipient_id or not recipient_id.isdigit():
                print(f"⚠️ تم تجاهل حدث بسبب recipient_id غير صالح (يجب أن يكون أرقام فقط): {recipient_id}")
                continue

            # البحث عن التاجر صاحب هذه الصفحة أو هذا الحساب
            rows = (
                supabase.table("tenants")
                .select("id, name, bot_enabled, facebook_page_id, instagram_account_id, facebook_page_token")
                .or_(f"facebook_page_id.eq.{recipient_id},instagram_account_id.eq.{recipient_id}")
                .execute()
                .data
            )
            if not rows:
                print(f"⚠️ صفحة غير مسجلة في النظام: {recipient_id}")
                continue

            tenant = rows[0]
            if not tenant.get("bot_enabled", True):
                print(f"⏸️ البوت موقوف للتاجر «{tenant['name']}» (لم يدفع الاشتراك) — تم تجاهل الرسالة")
                continue

            meta_channel = "instagram" if str(tenant.get("instagram_account_id") or "") == str(recipient_id) else "messenger"
            channel_name = "instagram" if meta_channel == "instagram" else "facebook"
            if not is_channel_allowed(tenant["id"], channel_name, tenant):
                print(f"⏸️ قناة {channel_name} غير مفعّلة في اشتراك التاجر «{tenant['name']}» — تم تجاهل الرسالة")
                continue

            page_token = tenant.get("facebook_page_token", "")
            print(f"📩 رسالة جديدة للتاجر «{tenant['name']}» من العميل {sender_id}: {text}")

            # تشغيل الذكاء الاصطناعي بشكل غير معطل للـ Event Loop (non-blocking)
            reply = await asyncio.to_thread(handle_customer_message, tenant["id"], sender_id, text, channel=meta_channel)
            print(f"🤖 رد البوت: {reply}")

            # إرسال الرد للعميل
            if page_token and reply and reply.strip():
                await send_meta_reply(sender_id, reply, page_token)
                # صور المنتجات اللي البوت جهزها (لو العميل طلب يشوف الصورة)
                for img_url in pop_customer_images(tenant["id"], meta_channel, sender_id):
                    await send_meta_image(sender_id, img_url, page_token, platform=meta_channel)
            elif not page_token:
                print(f"⚠️ التاجر «{tenant['name']}» مش عنده page_token مسجّل — استخدم /link_page في تليجرام")

        # ب) رسائل واتساب وتعليقات فيسبوك
        changes = entry.get("changes", [])
        for change in changes:
            field = change.get("field")
            value = change.get("value", {})

            # 1. تعليقات فيسبوك
            if field == "feed" and value.get("item") == "comment" and value.get("verb") == "add":
                comment_id = value.get("comment_id")
                post_id = value.get("post_id")
                parent_id = value.get("parent_id")
                sender_id = value.get("from", {}).get("id") if isinstance(value.get("from"), dict) else None
                sender_name = value.get("from", {}).get("name", "يا فندم") if isinstance(value.get("from"), dict) else "يا فندم"
                comment_text = (value.get("message") or "").strip()
                page_id = str(entry.get("id") or "").strip()

                if not page_id or not page_id.isdigit():
                    print(f"⚠️ تم تجاهل تعليق فيسبوك بسبب page_id غير صالح: {page_id}")
                    continue

                # شرط 1: تجاهل لو from.id فاضي، وتجاهل أي تعليق للصفحة نفسها
                if not sender_id or str(sender_id).strip() == "" or str(sender_id).strip() == page_id:
                    continue

                if not comment_text and not value.get("attachment") and not value.get("photo"):
                    continue

                # منع تكرار الرد على نفس تعليق فيسبوك
                if comment_id and DEDUPLICATOR.is_duplicate(f"fb_comment:{comment_id}"):
                    print(f"⏩ تم تجاهل تعليق فيسبوك مكرر (id: {comment_id})")
                    continue

                rows = (
                    supabase.table("tenants")
                    .select("id, name, bot_enabled, facebook_page_token")
                    .eq("facebook_page_id", page_id)
                    .execute()
                    .data
                )
                if not rows:
                    continue

                tenant = rows[0]
                if not tenant.get("bot_enabled", True):
                    print(f"⏸️ البوت موقوف للتاجر «{tenant['name']}» — تم تجاهل تعليق فيسبوك")
                    continue

                if not is_channel_allowed(tenant["id"], "facebook", tenant):
                    print(f"⏸️ قناة فيسبوك غير مفعّلة في اشتراك التاجر «{tenant['name']}» — تم تجاهل تعليق فيسبوك")
                    continue

                page_token = tenant.get("facebook_page_token", "")
                if not page_token:
                    print(f"⚠️ التاجر «{tenant['name']}» مش عنده page_token — استخدم /link_page")
                    continue

                # شرط 1: تجاهل التعليق لو parent_id مختلف عن post_id (رد على تعليق)، إلا لو التعليق الأب كاتبه page_id
                if parent_id and post_id and str(parent_id).strip() != str(post_id).strip():
                    is_page_parent = await is_parent_comment_from_page(str(parent_id).strip(), page_id, page_token)
                    if not is_page_parent:
                        print(f"⏩ تم تجاهل رد عميل على تعليق عميل آخر (parent_id: {parent_id})")
                        continue

                # شرط 6: سقف يومي منفصل للتعليقات
                is_allowed, _ = RATE_LIMITER.check_comment_rate_limit(tenant["id"], sender_id)
                if not is_allowed:
                    print(f"⏸️ تم تخطي التعليق بسبب بلوغ السقف اليومي لردود التعليقات للتاجر «{tenant['name']}»")
                    continue

                # شرط 7: قبل الرد، افحص إن مفيش رد من الصفحة على نفس التعليق (Graph API)
                if await has_page_already_replied(comment_id, page_id, page_token):
                    print(f"⏩ تم تخطي التعليق: الصفحة ردت عليه مسبقاً ({comment_id})")
                    continue

                # شرط 4: جلب نص البوست وصورة التعليق إن وجدت
                post_text = await fetch_post_text(post_id, page_token)
                img_bytes, img_mime = await fetch_comment_image(value, comment_id, page_token)

                # فحص الردود المتكررة في الكاش لتوفير التوكنز (كاش 5 دقائق)
                intent = normalize_comment_intent(comment_text)
                if intent:
                    cached = COMMENT_CACHE.get(tenant["id"], post_id, intent, sender_name=sender_name)
                    if cached:
                        cached_pub, cached_priv = cached
                        if cached_pub:
                            await send_public_comment_reply(comment_id, cached_pub, page_token)
                        if cached_priv:
                            await send_meta_reply(sender_id, cached_priv, page_token, platform="messenger", comment_id=comment_id)
                        print(f"⚡ [توفير توكنز] تم الرد من الكاش (5 دقائق) للعميل «{sender_name}» على فيسبوك بنية [{intent}] (0 توكنز)!")
                        continue

                    # اختصارات صفر توكنز للنيات المحددة
                    if intent == "INTENT_CONFIRM":
                        confirm_tpl = "تسلم يا {name}! نورتنا وشرفتنا، ولو محتاج أي استفسار إحنا معاك دائماً ❤️"
                        await send_public_comment_reply(comment_id, confirm_tpl.format(name=sender_name), page_token)
                        COMMENT_CACHE.set(tenant["id"], post_id, intent, confirm_tpl)
                        continue

                    if intent == "INTENT_LOCATION":
                        try:
                            branches = supabase.table("branches").select("name, city, address, working_hours").eq("tenant_id", tenant["id"]).execute().data
                        except Exception:
                            branches = []
                        if branches:
                            b_details = " - ".join([f"{b.get('name', 'فرع')}: {b.get('address', '')} ({b.get('working_hours', 'طوال الأسبوع')})" for b in branches])
                            loc_tpl = f"أهلاً {{name}} ❤️ تشرفنا زيارتك في {b_details}. ونشرف بيك في أي وقت 🙏"
                            await send_public_comment_reply(comment_id, loc_tpl.format(name=sender_name), page_token)
                            COMMENT_CACHE.set(tenant["id"], post_id, intent, loc_tpl)
                            print(f"⚡ [توفير توكنز] تم الرد بعنوان الفرع مباشرة للعميل «{sender_name}» (0 توكنز)!")
                            continue

                # شرط 2: classify_comment(text, post_text)
                category = await asyncio.to_thread(classify_comment, comment_text, post_text)
                print(f"💬 تعليق فيسبوك على «{tenant['name']}» من {sender_name}: [{category}] - {comment_text}")

                # شرط 3: تطبيق سياسة الرد حسب التصنيف
                if category == "skip":
                    print(f"⏩ تم تجاهل التعليق المصنف skip: {comment_text}")
                    continue

                elif category == "complaint":
                    # complaint = إشعار التاجر على تليجرام بدون رد علني
                    await asyncio.to_thread(
                        send_complaint_notification,
                        tenant["id"],
                        customer_name=sender_name,
                        comment_text=comment_text,
                        post_text=post_text,
                        comment_id=comment_id,
                        platform="facebook",
                    )
                    print(f"🚨 تم إرسال إشعار شكوى للتاجر «{tenant['name']}» على تليجرام بدون رد علني.")
                    continue

                elif category == "praise":
                    # praise = شكر + submit_customer_review
                    praise_reply = f"شكراً لذوقك وكلامك الجميل يا {sender_name}! ❤️ تسعدنا خدمتك دايماً وسعداء جداً بوجودك معانا 🙏"
                    await send_public_comment_reply(comment_id, praise_reply, page_token)
                    await asyncio.to_thread(submit_praise_review, tenant["id"], sender_name, comment_text)
                    continue

                elif category == "order":
                    # order = الخاص بس
                    if await asyncio.to_thread(is_shipping_enabled, tenant["id"]):
                        order_private_msg = (
                            f"أهلاً بحضرتك يا فندم! 🌸 يسعدنا جداً تنفيذ طلبك.\n"
                            f"علشان نسجل الأوردر ويوصلك في أسرع وقت، يرجى إرسال بياناتك هنا في الخاص:\n"
                            f"• الاسم ثلاثي\n• رقم الموبايل\n• المحافظة والعنوان بالتفصيل\n"
                            f"والدفع عند الاستلام مع إمكانية المعاينة قبل الاستلام 🙏"
                        )
                    else:
                        order_private_msg = (
                            f"أهلاً بحضرتك يا فندم! 🌸 يسعدنا جداً تنفيذ طلبك.\n"
                            f"علشان نحجزلك الأوردر، يرجى إرسال بياناتك هنا في الخاص:\n"
                            f"• الاسم\n• رقم الموبايل\n"
                            f"والاستلام من المحل والدفع عند الاستلام مع إمكانية المعاينة والقياس 🙏"
                        )
                    await send_meta_reply(sender_id, order_private_msg, page_token, platform="messenger", comment_id=comment_id)
                    continue

                elif category in ("price", "availability"):
                    # price/availability = رد علني قصير + دعوة للخاص (أو Private Reply لو متاح)
                    public_teaser_tpl = "أهلاً {name} ❤️ تم إرسال كافة التفاصيل والأسعار والمقاسات لحضرتك في رسائل الصفحة على الخاص (شيك على الـ DM) 📩🙏"
                    await send_public_comment_reply(comment_id, public_teaser_tpl.format(name=sender_name), page_token)

                    prompt_ctx = f"العميل علّق على بوست نصه: '{post_text}' وسأل في التعليق: '{comment_text}'. أجب على استفساره بخصوص السعر أو المقاسات والألوان المتاحة باختصار ولطف."
                    detailed_reply = await asyncio.to_thread(
                        handle_customer_message,
                        tenant["id"], sender_id,
                        prompt_ctx,
                        image_bytes=img_bytes,
                        image_mime_type=img_mime,
                        channel="messenger",
                        allow_orders=False,
                        is_comment=True,
                    )
                    if detailed_reply and detailed_reply.strip():
                        await send_meta_reply(sender_id, detailed_reply.strip(), page_token, platform="messenger", comment_id=comment_id)
                        # تخزين في الكاش 5 دقائق للكومنتات المتكررة القادمة
                        COMMENT_CACHE.set(tenant["id"], post_id, intent or "INTENT_DETAILS", public_teaser_tpl, detailed_reply.strip())
                    continue

                elif category in ("location", "hours", "shipping", "offer"):
                    # location/hours/shipping/offer = رد علني بالإجابة
                    prompt_ctx = f"العميل علّق على بوست نصه: '{post_text}' وسأل في التعليق: '{comment_text}'. أجب مباشرة وبشكل علني على استفساره بدقة ولطف من بيانات وسياسات المتجر."
                    public_answer = await asyncio.to_thread(
                        handle_customer_message,
                        tenant["id"], sender_id,
                        prompt_ctx,
                        image_bytes=img_bytes,
                        image_mime_type=img_mime,
                        channel="messenger",
                        allow_orders=False,
                        is_comment=True,
                    )
                    if public_answer and public_answer.strip():
                        await send_public_comment_reply(comment_id, public_answer.strip(), page_token)
                        if intent:
                            COMMENT_CACHE.set(tenant["id"], post_id, intent, public_answer.strip())
                    continue

            # 1b. تعليقات إنستجرام
            elif field == "comments" and value.get("verb") == "add":
                comment_id = value.get("id")
                sender_id = value.get("from", {}).get("id") if isinstance(value.get("from"), dict) else None
                sender_name = value.get("from", {}).get("name", "يا فندم") if isinstance(value.get("from"), dict) else "يا فندم"
                comment_text = (value.get("text") or "").strip()
                ig_media_id = value.get("media", {}).get("id", "")
                parent_id = value.get("parent_id")
                page_id = str(entry.get("id") or "").strip()

                if not page_id or not page_id.isdigit():
                    print(f"⚠️ تم تجاهل تعليق إنستجرام بسبب page_id غير صالح: {page_id}")
                    continue

                # شرط 1: تجاهل لو from.id فاضي أو تعليق للصفحة نفسها
                if not sender_id or str(sender_id).strip() == "" or str(sender_id).strip() == page_id:
                    continue

                if not comment_text or not comment_id:
                    continue

                # منع تكرار الرد على نفس تعليق إنستجرام
                if comment_id and DEDUPLICATOR.is_duplicate(f"ig_comment:{comment_id}"):
                    print(f"⏩ تم تجاهل كومنت إنستجرام مكرر (id: {comment_id})")
                    continue

                rows = (
                    supabase.table("tenants")
                    .select("id, name, bot_enabled, facebook_page_token")
                    .eq("instagram_account_id", page_id)
                    .execute()
                    .data
                )
                if not rows:
                    continue

                tenant = rows[0]
                if not tenant.get("bot_enabled", True):
                    print(f"⏸️ البوت موقوف للتاجر «{tenant['name']}» — تم تجاهل تعليق إنستجرام")
                    continue

                if not is_channel_allowed(tenant["id"], "instagram", tenant):
                    print(f"⏸️ قناة إنستجرام غير مفعّلة في اشتراك التاجر «{tenant['name']}» — تم تجاهل تعليق إنستجرام")
                    continue

                page_token = tenant.get("facebook_page_token", "")
                if not page_token:
                    print(f"⚠️ التاجر «{tenant['name']}» مش عنده page_token")
                    continue

                # شرط 1: تجاهل لو parent_id رد عميل على عميل آخر
                if parent_id and ig_media_id and str(parent_id).strip() != str(ig_media_id).strip():
                    is_page_parent = await is_parent_comment_from_page(str(parent_id).strip(), page_id, page_token)
                    if not is_page_parent:
                        print(f"⏩ تم تجاهل رد عميل على تعليق عميل آخر في إنستجرام (parent_id: {parent_id})")
                        continue

                # شرط 6: سقف يومي منفصل للتعليقات
                is_allowed, _ = RATE_LIMITER.check_comment_rate_limit(tenant["id"], sender_id)
                if not is_allowed:
                    print(f"⏸️ تم تخطي تعليق إنستجرام بسبب سقف التعليقات للتاجر «{tenant['name']}»")
                    continue

                # شرط 7: فحص إن مفيش رد من الصفحة مسبقاً
                if await has_ig_already_replied(comment_id, page_id, page_token):
                    print(f"⏩ تم تخطي تعليق إنستجرام: تم الرد عليه مسبقاً ({comment_id})")
                    continue

                # شرط 4: جلب كابشن المنشور
                post_text = ""
                if ig_media_id:
                    post_text = await fetch_post_text(ig_media_id, page_token)

                # فحص الردود المتكررة في الكاش لتوفير التوكنز في إنستجرام
                intent = normalize_comment_intent(comment_text)
                if intent:
                    cached = COMMENT_CACHE.get(tenant["id"], ig_media_id, intent, sender_name=sender_name)
                    if cached:
                        cached_pub, _ = cached
                        if cached_pub:
                            await send_instagram_comment_reply(comment_id, cached_pub, page_token)
                        print(f"⚡ [توفير توكنز] تم الرد من الكاش (5 دقائق) للعميل «{sender_name}» على إنستجرام بنية [{intent}] (0 توكنز)!")
                        continue

                    if intent == "INTENT_CONFIRM":
                        confirm_tpl = "تسلم يا {name}! نورتنا وشرفتنا، ولو محتاج أي استفسار إحنا معاك دائماً ❤️"
                        await send_instagram_comment_reply(comment_id, confirm_tpl.format(name=sender_name), page_token)
                        COMMENT_CACHE.set(tenant["id"], ig_media_id, intent, confirm_tpl)
                        continue

                    if intent == "INTENT_LOCATION":
                        try:
                            branches = supabase.table("branches").select("name, city, address, working_hours").eq("tenant_id", tenant["id"]).execute().data
                        except Exception:
                            branches = []
                        if branches:
                            b_details = " - ".join([f"{b.get('name', 'فرع')}: {b.get('address', '')} ({b.get('working_hours', 'طوال الأسبوع')})" for b in branches])
                            loc_tpl = f"أهلاً {{name}} ❤️ تشرفنا زيارتك في {b_details}. ونشرف بيك في أي وقت 🙏"
                            await send_instagram_comment_reply(comment_id, loc_tpl.format(name=sender_name), page_token)
                            COMMENT_CACHE.set(tenant["id"], ig_media_id, intent, loc_tpl)
                            print(f"⚡ [توفير توكنز] تم الرد بعنوان الفرع على إنستجرام مباشرة (0 توكنز)!")
                            continue

                # شرط 2: classify_comment
                category = await asyncio.to_thread(classify_comment, comment_text, post_text)
                print(f"📸 كومنت إنستجرام على «{tenant['name']}» من {sender_name}: [{category}] - {comment_text}")

                if category == "skip":
                    print(f"⏩ تم تخطي كومنت إنستجرام المصنف skip: {comment_text}")
                    continue

                elif category == "complaint":
                    await asyncio.to_thread(
                        send_complaint_notification,
                        tenant["id"],
                        customer_name=sender_name,
                        comment_text=comment_text,
                        post_text=post_text,
                        comment_id=comment_id,
                        platform="instagram",
                    )
                    print(f"🚨 تم إرسال إشعار شكوى إنستجرام للتاجر «{tenant['name']}» بدون رد علني.")
                    continue

                elif category == "praise":
                    praise_reply = f"شكراً لذوقك وكلامك الجميل يا {sender_name}! ❤️ تسعدنا خدمتك دايماً 🙏"
                    await send_instagram_comment_reply(comment_id, praise_reply, page_token)
                    await asyncio.to_thread(submit_praise_review, tenant["id"], sender_name, comment_text)
                    continue

                elif category == "order":
                    _data_hint = "(الاسم، العنوان، الموبايل)" if await asyncio.to_thread(is_shipping_enabled, tenant["id"]) else "(الاسم والموبايل)"
                    order_reply = f"أهلاً {sender_name} ❤️ يسعدنا طلبك! يرجى إرسال بياناتك {_data_hint} في الرسائل الخاصة (DM) لتأكيد الأوردر فوراً 📩"
                    await send_instagram_comment_reply(comment_id, order_reply, page_token)
                    continue

                elif category in ("price", "availability"):
                    dm_teaser_tpl = "أهلاً {name} ❤️ تم إرسال كافة التفاصيل والأسعار والمقاسات لحضرتك في الـ DM، شيك على الرسائل الخاصة 📩🙏"
                    await send_instagram_comment_reply(comment_id, dm_teaser_tpl.format(name=sender_name), page_token)
                    COMMENT_CACHE.set(tenant["id"], ig_media_id, intent or "INTENT_DETAILS", dm_teaser_tpl)
                    continue

                elif category in ("location", "hours", "shipping", "offer"):
                    prompt_ctx = f"العميل علّق على منشور إنستجرام نصه: '{post_text}' وسأل في التعليق: '{comment_text}'. أجب مباشرة وبشكل علني على استفساره بدقة ولطف من بيانات وسياسات المتجر."
                    public_reply = await asyncio.to_thread(
                        handle_customer_message,
                        tenant["id"], sender_id,
                        prompt_ctx,
                        channel="instagram",
                        allow_orders=False,
                        is_comment=True,
                    )
                    if public_reply and public_reply.strip():
                        await send_instagram_comment_reply(comment_id, public_reply.strip(), page_token)
                        if intent:
                            COMMENT_CACHE.set(tenant["id"], ig_media_id, intent, public_reply.strip())
                    continue



            # 2. رسائل واتساب
            elif field == "messages":
                phone_number_id = str(value.get("metadata", {}).get("phone_number_id") or "").strip()
                if not phone_number_id or not phone_number_id.isdigit():
                    print(f"⚠️ تم تجاهل رسالة واتساب بسبب phone_number_id غير صالح: {phone_number_id}")
                    continue
                messages = value.get("messages", [])
                for msg in messages:
                    msg_type = msg.get("type")
                    wa_mid = msg.get("id")
                    wa_from = msg.get("from")

                    if msg_type == "text":
                        wa_text = msg.get("text", {}).get("body")
                        if not wa_text:
                            continue

                        # منع تكرار معالجة رسائل واتساب المكررة
                        if wa_mid and DEDUPLICATOR.is_duplicate(f"wa_msg:{wa_mid}"):
                            print(f"⏩ تم تجاهل رسالة واتساب مكررة (id: {wa_mid})")
                            continue

                        rows = (
                            supabase.table("tenants")
                            .select("id, name, bot_enabled, whatsapp_phone_number_id, whatsapp_access_token")
                            .eq("whatsapp_phone_number_id", phone_number_id)
                            .execute()
                            .data
                        )
                        if rows:
                            tenant = rows[0]
                            if not tenant.get("bot_enabled", True):
                                print(f"⏸️ البوت موقوف للتاجر «{tenant['name']}» — تم تجاهل رسالة واتساب")
                                continue

                            if not is_channel_allowed(tenant["id"], "whatsapp", tenant):
                                print(f"⏸️ قناة الواتساب غير مفعّلة في اشتراك التاجر «{tenant['name']}» — تم تجاهل رسالة واتساب")
                                continue

                            wa_allowed, wa_used, wa_limit = check_and_increment_whatsapp_usage(tenant["id"], tenant)
                            if not wa_allowed:
                                print(f"⚠️ التاجر «{tenant['name']}» استهلك باقة الواتساب بالكامل ({wa_used}/{wa_limit})")
                                continue

                            wa_token = tenant.get("whatsapp_access_token", "")
                            # تشغيل الـ AI بشكل غير معطل للـ Event Loop
                            reply = await asyncio.to_thread(handle_customer_message, tenant["id"], wa_from, wa_text, channel="whatsapp")
                            print(f"🟢 واتساب «{tenant['name']}» -> رد للعميل {wa_from}: {reply}")
                            if wa_token and phone_number_id and reply and reply.strip():
                                await send_whatsapp_reply(wa_from, reply, phone_number_id, wa_token)
                                for img_url in pop_customer_images(tenant["id"], "whatsapp", wa_from):
                                    await send_whatsapp_image(wa_from, img_url, phone_number_id, wa_token)
                            elif not wa_token:
                                print(f"⚠️ التاجر «{tenant['name']}» مش عنده wa_token — استخدم /link_page wa_token=...")

                    elif msg_type in ("audio", "voice", "image"):
                        # منع تكرار معالجة رسائل الصوت أو الصور
                        if wa_mid and DEDUPLICATOR.is_duplicate(f"wa_media:{wa_mid}"):
                            continue

                        rows = (
                            supabase.table("tenants")
                            .select("id, name, bot_enabled, whatsapp_phone_number_id, whatsapp_access_token")
                            .eq("whatsapp_phone_number_id", phone_number_id)
                            .execute()
                            .data
                        )
                        if rows:
                            tenant = rows[0]
                            if not tenant.get("bot_enabled", True):
                                continue
                            if not is_channel_allowed(tenant["id"], "whatsapp", tenant):
                                continue
                            wa_token = tenant.get("whatsapp_access_token", "")
                            if wa_token and phone_number_id and wa_from:
                                # تطبيق حد المعدل على ردود وسائط الواتساب (رد مرة واحدة كل دقيقة ثم تجاهل)
                                is_allowed, rate_msg = RATE_LIMITER.check_rate_limit(tenant["id"], wa_from)
                                if is_allowed:
                                    reply = "ابعتلي رسالتك كتابة لو سمحت 🙏"
                                else:
                                    reply = rate_msg

                                if reply and reply.strip():
                                    print(f"🟢 واتساب «{tenant['name']}» ({msg_type}) -> رد للعميل {wa_from}: {reply}")
                                    await send_whatsapp_reply(wa_from, reply, phone_number_id, wa_token)



@app.post("/webhook/meta")
async def receive_meta_webhook(request: Request, background_tasks: BackgroundTasks):
    """استقبال رسائل فيسبوك، إنستجرام، واتساب في الخلفية بعد فحص توقيع X-Hub-Signature-256"""
    raw_body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(raw_body, sig):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid X-Hub-Signature-256")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    background_tasks.add_task(process_meta_event, payload)
    return {"status": "received"}


# ==========================================
# 2. TikTok Webhook
# ==========================================

async def send_tiktok_comment_reply(video_id: str, comment_id: str, text: str, access_token: str):
    """الرد على كومنت تيك توك عبر TikTok Open API v2 مع حماية الـ Rate Limiting (550 طلب/دقيقة)"""
    await TIKTOK_RATE_LIMITER.acquire()
    url = "https://open.tiktokapis.com/v2/comment/reply/create/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    payload = {
        "video_id": video_id,
        "parent_comment_id": comment_id,
        "text": text,
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code == 429:
                TIKTOK_RATE_LIMITER.record_rate_limited(60.0)
            data = r.json()
            if data.get("error", {}).get("code") == "ok":
                print(f"✅ تم الرد على كومنت تيك توك {comment_id}")
            elif "rate_limit" in str(data.get("error", {}).get("message", "")).lower() or data.get("error", {}).get("code") == "rate_limit_exceeded":
                TIKTOK_RATE_LIMITER.record_rate_limited(60.0)
                print(f"⚠️ [TikTok Rate Limit] رد تيك توك بتجاوز السقف:", data.get("error"))
            else:
                print(f"❌ خطأ في الرد على تيك توك:", data.get("error"))
        except Exception as e:
            print(f"❌ استثناء تيك توك:", type(e).__name__)


async def send_tiktok_dm(user_id: str, text: str, access_token: str):
    """إرسال رسالة مباشرة DM على تيك توك (TikTok Business Messaging) مع حماية الـ Rate Limiting"""
    await TIKTOK_RATE_LIMITER.acquire()
    url = "https://open.tiktokapis.com/v2/direct_message/send/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    payload = {
        "to_user_id": user_id,
        "message_type": "text",
        "content": {"text": text},
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code == 429:
                TIKTOK_RATE_LIMITER.record_rate_limited(60.0)
            data = r.json()
            if data.get("error", {}).get("code") == "ok":
                print(f"✅ DM تيك توك اتبعت للعميل {user_id}")
            elif "rate_limit" in str(data.get("error", {}).get("message", "")).lower() or data.get("error", {}).get("code") == "rate_limit_exceeded":
                TIKTOK_RATE_LIMITER.record_rate_limited(60.0)
                print(f"⚠️ [TikTok Rate Limit] رد تيك توك DM بتجاوز السقف:", data.get("error"))
            else:
                print(f"❌ خطأ في DM تيك توك:", data.get("error"))
        except Exception as e:
            print(f"❌ استثناء DM تيك توك:", type(e).__name__)


async def process_tiktok_event(data: dict):
    """معالجة أحداث تيك توك (كومنتات ورسائل مباشرة)"""
    event_type = data.get("event")
    print(f"🎵 حدث تيك توك: {event_type}")

    # ───── كومنتات تيك توك ─────
    if event_type == "comment.create":
        video_id    = data.get("video_id", "")
        comment_id  = data.get("comment_id", "")
        user_id     = data.get("user_id", "")
        display_name = data.get("display_name", "يا فندم")
        comment_text = data.get("text", "")
        account_id  = str(data.get("owner_id") or "").strip()   # معرف صاحب الفيديو (التاجر)

        if not comment_text or not account_id or not re.match(r"^[a-zA-Z0-9_\-]+$", account_id):
            return

        # البحث عن التاجر بـ tiktok_account_id
        rows = (
            supabase.table("tenants")
            .select("id, name, bot_enabled, tiktok_access_token")
            .eq("tiktok_account_id", account_id)
            .execute()
            .data
        )
        if not rows:
            print(f"⚠️ حساب تيك توك غير مسجّل: {account_id}")
            return

        tenant = rows[0]
        if not tenant.get("bot_enabled", True):
            print(f"⏸️ البوت موقوف للتاجر «{tenant['name']}» — تم تجاهل كومنت تيك توك")
            return

        if not is_channel_allowed(tenant["id"], "tiktok", tenant):
            print(f"⏸️ قناة تيك توك غير مفعّلة في اشتراك التاجر «{tenant['name']}» — تم تجاهل كومنت تيك توك")
            return

        # منع تكرار كومنت تيك توك
        if comment_id and DEDUPLICATOR.is_duplicate(f"tiktok_comment:{comment_id}"):
            print(f"⏩ تم تجاهل كومنت تيك توك مكرر (id: {comment_id})")
            return

        # فحص سقف التعليقات اليومي المنفصل
        is_allowed, _ = RATE_LIMITER.check_comment_rate_limit(tenant["id"], user_id)
        if not is_allowed:
            print(f"⏸️ تم تخطي كومنت تيك توك بسبب بلوغ سقف التعليقات للتاجر «{tenant['name']}»")
            return

        tiktok_token = tenant.get("tiktok_access_token", "")
        print(f"🎵 كومنت على «{tenant['name']}» من {display_name}: {comment_text}")

        # فحص الكاش للردود المتكررة في تيك توك لتوفير التوكنز
        intent = normalize_comment_intent(comment_text)
        if intent:
            cached = COMMENT_CACHE.get(tenant["id"], video_id, intent, sender_name=display_name)
            if cached:
                cached_pub, _ = cached
                if cached_pub and tiktok_token and video_id and comment_id:
                    await send_tiktok_comment_reply(video_id, comment_id, cached_pub, tiktok_token)
                print(f"⚡ [توفير توكنز] تم الرد من الكاش (5 دقائق) للعميل «{display_name}» على تيك توك بنية [{intent}] (0 توكنز)!")
                return

            if intent == "INTENT_CONFIRM":
                confirm_tpl = "تسلم يا {name}! نورتنا وشرفتنا دائماً ❤️"
                if tiktok_token and video_id and comment_id:
                    await send_tiktok_comment_reply(video_id, comment_id, confirm_tpl.format(name=display_name), tiktok_token)
                COMMENT_CACHE.set(tenant["id"], video_id, intent, confirm_tpl)
                return

            if intent == "INTENT_LOCATION":
                try:
                    branches = supabase.table("branches").select("name, city, address, working_hours").eq("tenant_id", tenant["id"]).execute().data
                except Exception:
                    branches = []
                if branches:
                    b_details = " - ".join([f"{b.get('name', 'فرع')}: {b.get('address', '')} ({b.get('working_hours', 'طوال الأسبوع')})" for b in branches])
                    loc_tpl = f"أهلاً {{name}} ❤️ تشرفنا زيارتك في {b_details}. ونشرف بيك في أي وقت 🙏"
                    if tiktok_token and video_id and comment_id:
                        await send_tiktok_comment_reply(video_id, comment_id, loc_tpl.format(name=display_name), tiktok_token)
                    COMMENT_CACHE.set(tenant["id"], video_id, intent, loc_tpl)
                    print(f"⚡ [توفير توكنز] تم الرد بعنوان الفرع على تيك توك مباشرة (0 توكنز)!")
                    return

        video_title = data.get("video_title") or data.get("title") or data.get("item_title") or ""

        # تصنيف التعليق
        category = await asyncio.to_thread(classify_comment, comment_text, video_title)
        if category == "skip":
            print(f"⏩ تم تخطي كومنت تيك توك المصنف skip: {comment_text}")
            return

        elif category == "complaint":
            await asyncio.to_thread(
                send_complaint_notification,
                tenant["id"],
                customer_name=display_name,
                comment_text=comment_text,
                post_text=video_title,
                comment_id=comment_id,
                platform="tiktok",
            )
            print(f"🚨 تم إرسال إشعار شكوى تيك توك للتاجر «{tenant['name']}» بدون رد علني.")
            return

        elif category == "praise":
            praise_msg = f"شكراً لذوقك وكلامك الجميل يا {display_name}! ❤️ تسعدنا خدمتك دايماً 🙏"
            if tiktok_token and video_id and comment_id:
                await send_tiktok_comment_reply(video_id, comment_id, praise_msg, tiktok_token)
            await asyncio.to_thread(submit_praise_review, tenant["id"], display_name, comment_text)
            return

        elif category == "order":
            _what = "بيانات الشحن" if await asyncio.to_thread(is_shipping_enabled, tenant["id"]) else "بيانات الاستلام من المحل"
            order_reply = f"أهلاً {display_name} ❤️ يسعدنا طلبك! يرجى التواصل معنا عبر الرسائل الخاصة (DM) لتأكيد الأوردر و{_what} فوراً 📩"
            if tiktok_token and video_id and comment_id:
                await send_tiktok_comment_reply(video_id, comment_id, order_reply, tiktok_token)
            return

        elif category in ("price", "availability"):
            teaser_tpl = "أهلاً {name} ❤️ تم إرسال كافة التفاصيل والأسعار والمقاسات في الرسائل الخاصة، شيك على الخاص 📩🙏"
            if tiktok_token and video_id and comment_id:
                await send_tiktok_comment_reply(video_id, comment_id, teaser_tpl.format(name=display_name), tiktok_token)
            COMMENT_CACHE.set(tenant["id"], video_id, intent or "INTENT_DETAILS", teaser_tpl)
            return

        elif category in ("location", "hours", "shipping", "offer"):
            prompt_ctx = f"العميل علّق على فيديو تيك توك عنوانه '{video_title}': {comment_text}" if video_title else f"علّقت على فيديو تيك توك وكتبت: {comment_text}"
            full_reply = await asyncio.to_thread(
                handle_customer_message,
                tenant["id"], user_id,
                prompt_ctx,
                channel="tiktok",
                allow_orders=False,
                is_comment=True,
            )
            if tiktok_token and video_id and comment_id and full_reply and full_reply.strip():
                await send_tiktok_comment_reply(video_id, comment_id, full_reply.strip(), tiktok_token)
                if intent:
                    COMMENT_CACHE.set(tenant["id"], video_id, intent, full_reply.strip())
            elif not tiktok_token:
                print(f"⚠️ التاجر «{tenant['name']}» مش عنده tiktok_token — استخدم /link_page")

    # ───── رسائل DM تيك توك ─────
    elif event_type == "direct_message.create":
        sender_id    = data.get("sender_id", "")
        display_name = data.get("sender_display_name", "يا فندم")
        message_text = data.get("message", {}).get("text", "")
        msg_id       = data.get("message_id") or data.get("id", "")
        account_id   = str(data.get("receiver_id") or "").strip()   # حساب التاجر المستقبِل

        if not message_text or not account_id or not re.match(r"^[a-zA-Z0-9_\-]+$", account_id):
            return

        # منع تكرار رسالة تيك توك DM
        if msg_id and DEDUPLICATOR.is_duplicate(f"tiktok_dm:{msg_id}"):
            print(f"⏩ تم تجاهل رسالة DM تيك توك مكررة (id: {msg_id})")
            return

        rows = (
            supabase.table("tenants")
            .select("id, name, bot_enabled, tiktok_access_token")
            .eq("tiktok_account_id", account_id)
            .execute()
            .data
        )
        if not rows:
            print(f"⚠️ حساب تيك توك غير مسجّل: {account_id}")
            return

        tenant = rows[0]
        if not tenant.get("bot_enabled", True):
            print(f"⏸️ البوت موقوف للتاجر «{tenant['name']}» — تم تجاهل DM تيك توك")
            return

        if not is_channel_allowed(tenant["id"], "tiktok", tenant):
            print(f"⏸️ قناة تيك توك غير مفعّلة في اشتراك التاجر «{tenant['name']}» — تم تجاهل DM تيك توك")
            return

        tiktok_token = tenant.get("tiktok_access_token", "")
        print(f"🎵 DM تيك توك لـ «{tenant['name']}» من {display_name}: {message_text}")

        reply = await asyncio.to_thread(handle_customer_message, tenant["id"], sender_id, message_text, channel="tiktok")

        if tiktok_token and reply and reply.strip():
            await send_tiktok_dm(sender_id, reply.strip(), tiktok_token)
        elif not tiktok_token:
            print(f"⚠️ التاجر «{tenant['name']}» مش عنده tiktok_token")


@app.get("/webhook/tiktok")
def verify_tiktok_webhook(
    challenge: str = Query(None),
    timestamp: str = Query(None),
):
    """التحقق من TikTok Webhook (يرجع الـ challenge كما هو)"""
    if challenge:
        print("✅ تم التحقق من TikTok Webhook!")
        return Response(content=challenge, media_type="text/plain")
    return {"status": "ok"}


@app.post("/webhook/tiktok")
async def receive_tiktok_webhook(request: Request, background_tasks: BackgroundTasks):
    """استقبال أحداث تيك توك في الخلفية بعد التحقق من التوقيع"""
    raw_body = await request.body()
    sig = (
        request.headers.get("TikTok-Signature")
        or request.headers.get("X-TikTok-Signature")
        or request.headers.get("X-Signature")
    )
    if not verify_tiktok_signature(raw_body, sig):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid TikTok webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    background_tasks.add_task(process_tiktok_event, payload)
    return {"status": "received"}


# ===============================================================
# 📢 Auto-Posting & Marketing Generator Functions
# ===============================================================

def generate_autopost_copy(product_name: str, price: float, material: str = "", colors: list = None, sizes: list = None, offer_text: str = "") -> str:
    """توليد كابشن إعلاني جذاب باللهجة المصرية لبوست فيسبوك باستخدام Gemini"""
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return f"🔥 شياكة وأناقة لا تقاوم! {product_name} متوفر الآن بسعر {price:.0f} جنيه فقط. اكتب 'تم' في التعليقات أو ابعتلنا رسالة لحجز مقاسك فوراً والدفع عند الاستلام!"

    client = genai.Client(api_key=api_key)
    prompt = f"""أنت مسوق وبياع محترف في براند ملابس مصري شهير.
اكتب بوست تسويقي جذاب وممتع لفيسبوك باللهجة المصرية لمنتجنا:
- اسم المنتج: {product_name}
- السعر: {price:.0f} جنيه مصري
- الخامة: {material or 'خامة فاخرة مريحة وعالية الجودة معالجة ضد الانكماش'}
- الألوان المتاحة: {', '.join(colors) if colors else 'تشكيلة ألوان شيك'}
- المقاسات المتاحة: {', '.join(sizes) if sizes else 'جميع المقاسات'}
{f'- عرض خاص: {offer_text}' if offer_text else ''}

قواعد البوست:
1. ابدأ بهوك (Hook) يشد الانتباه مع إيموجي جذاب.
2. اذكر تفاصيل المنتج وخامته ومميزاته بأسلوب بياع مصري شاطر ومقنع.
3. وضّح إن متاح معاينة وقياس قبل الدفع والدفع عند الاستلام لباب البيت.
4. Call to Action واضح: "اكتب تم في الكومنتات أو ابعتلنا على رسائل الصفحة وهنحجزلك فوراً!"
5. اكتب البوست مباشرة بدون مقدمات أو فواصل أو تعليقات خارجية (3 إلى 5 سطور مميزة مع هاشتاجات خفيفة)."""

    try:
        resp = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=prompt,
        )
        return resp.text.strip()
    except Exception as e:
        print("Error generating autopost copy:", e)
        return (
            f"🔥 الأناقة والراحة في قطعة واحدة! {product_name} متوفر حالياً بسعر {price:.0f} جنيه {('بخامة ' + material) if material else ''}.\n\n"
            f"🚚 شحن سريع ومعاينة وقياس قبل الاستلام!\n"
            f"📩 اطلب دلوقتي في رسالة خاصة أو اكتب تم في التعليقات وهنرد عليك فوراً!"
        )


async def publish_facebook_photo_post(page_id: str, page_access_token: str, image_url: str, caption: str) -> dict:
    """نشر بوست صورة وكابشن على صفحة فيسبوك عبر Graph API"""
    if not page_id or not page_access_token or not image_url:
        return {"status": "error", "message": "بيانات النشر غير مكتملة (Page ID أو Token أو رابط الصورة)"}

    url = f"https://graph.facebook.com/v21.0/{page_id}/photos"
    payload = {
        "url": image_url,
        "caption": caption,
        "access_token": page_access_token,
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            r = await client.post(url, data=payload)
            res_json = r.json()
            if r.status_code == 200 and ("id" in res_json or "post_id" in res_json):
                post_id = res_json.get("post_id") or res_json.get("id")
                return {
                    "status": "success",
                    "post_id": post_id,
                    "url": f"https://facebook.com/{post_id}",
                    "caption": caption,
                    "image_url": image_url,
                }
            else:
                err_msg = res_json.get("error", {}).get("message") or r.text
                return {"status": "error", "message": f"فشل نشر البوست على فيسبوك: {err_msg}"}
        except Exception as e:
            return {"status": "error", "message": f"خطأ في الاتصال بفيسبوك: {e}"}


async def trigger_autopost_for_tenant(tenant_id: str) -> dict:
    """اختيار أفضل منتج متوفر بالمخزن وتوليد كابشن إعلاني ونشره على صفحة التاجر"""
    t_res = supabase.table("tenants").select("*").eq("id", tenant_id).execute()
    if not t_res.data:
        return {"status": "error", "message": "التاجر غير موجود"}
    tenant = t_res.data[0]
    page_id = tenant.get("facebook_page_id")
    page_token = tenant.get("facebook_page_token")
    if not page_id or not page_token:
        return {"status": "error", "message": "يجب ربط صفحة فيسبوك وإدخال التوكن أولاً لتفعيل النشر التلقائي"}

    prods = (
        supabase.table("products")
        .select("id, name, price, material, image_url")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .neq("image_url", "")
        .execute()
        .data or []
    )
    if not prods:
        return {"status": "error", "message": "لا توجد منتجات بها صور في المخزن لنشرها"}

    p_ids = [p["id"] for p in prods]
    vars_res = (
        supabase.table("variants")
        .select("product_id, size, color, available")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .gt("available", 0)
        .in_("product_id", p_ids)
        .execute()
        .data or []
    )
    if not vars_res:
        return {"status": "error", "message": "لا توجد منتجات بها كميات متوفرة للبيع حالياً"}

    prod_variants = {}
    for v in vars_res:
        pid = v["product_id"]
        if pid not in prod_variants:
            prod_variants[pid] = []
        prod_variants[pid].append(v)

    available_prods = [p for p in prods if p["id"] in prod_variants]
    if not available_prods:
        return {"status": "error", "message": "لا توجد منتجات متوفرة بالمخزن ولها صور"}

    import random
    chosen_prod = random.choice(available_prods)
    v_list = prod_variants[chosen_prod["id"]]
    colors = list(set(v.get("color") for v in v_list if v.get("color")))
    sizes = list(set(v.get("size") for v in v_list if v.get("size")))

    promos = orders.get_active_promotions(tenant_id)
    offer_text = promos[0].get("description", "") if promos else ""

    caption = await asyncio.to_thread(
        generate_autopost_copy,
        product_name=chosen_prod["name"],
        price=float(chosen_prod.get("price") or 0),
        material=chosen_prod.get("material") or "",
        colors=colors,
        sizes=sizes,
        offer_text=offer_text,
    )

    res = await publish_facebook_photo_post(
        page_id=page_id,
        page_access_token=page_token,
        image_url=chosen_prod["image_url"],
        caption=caption,
    )

    if res.get("status") == "success":
        try:
            supabase.table("tenants").update({"autopost_last_at": "now()"}).eq("id", tenant_id).execute()
        except Exception:
            pass

    return {**res, "product_name": chosen_prod["name"]}


# ===============================================================
# 🌐 Merchant Dashboard & Telegram Web App Endpoints
# ===============================================================

DASHBOARD_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")


class MerchantSettingsPayload(BaseModel):
    bot_enabled: Optional[bool] = None
    facebook_page_id: Optional[str] = None
    facebook_page_token: Optional[str] = None
    google_sheets_url: Optional[str] = None


class ToggleBotPayload(BaseModel):
    bot_enabled: Optional[bool] = None


class ToggleAutopostPayload(BaseModel):
    autopost_enabled: Optional[bool] = None
    autopost_interval_hours: Optional[int] = 24


class PromotionPayload(BaseModel):
    code: Optional[str] = ""
    promo_type: str  # 'free_shipping', 'percentage', 'fixed', 'voucher'
    discount_value: Optional[float] = 0.0
    min_order_items: Optional[int] = 1
    min_order_value: Optional[float] = 0.0
    target_items: Optional[str] = ""
    branch: Optional[str] = "كل الفروع"
    description: str


class StockAddPayload(BaseModel):
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    category: Optional[str] = "رجالي"
    material: Optional[str] = ""
    price: Optional[float] = 0.0
    size: str
    color: str
    qty: int = 1
    image_data: Optional[str] = None



class BranchPayload(BaseModel):
    name: str
    governorate: str
    address: str
    google_maps_url: Optional[str] = ""
    working_hours: Optional[str] = "من 12 ظهراً لـ 11 مساءً"
    phone: Optional[str] = ""


class ShippingRatePayload(BaseModel):
    governorate: Optional[str] = "جميع المحافظات"
    price: float
    is_flat: Optional[bool] = False


class ToggleShippingPayload(BaseModel):
    shipping_enabled: Optional[bool] = None


class ProductImagePayload(BaseModel):
    image_data: Optional[str] = None  # Base64 string / data-uri
    image_url: Optional[str] = None   # Direct image URL


class OrderStatusPayload(BaseModel):
    status: str  # 'shipped', 'delivered', 'confirmed', 'new', 'cancelled'
    reason: Optional[str] = ""


def resolve_tenant_for_dashboard(identifier: str):
    """البحث عن التاجر بالـ UUID أو بادئة الـ ID (8 حروف سداسية عشرية فأكثر) فقط لحماية خصوصية المتاجر ومنع الدخول بالاسم"""
    if not identifier:
        return None
    ident = str(identifier).strip()

    # 1. فحص UUID كامل
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", ident):
        try:
            res = supabase.table("tenants").select("*").eq("id", ident).execute()
            if res.data:
                return res.data[0]
        except Exception:
            pass

    # 2. فحص بادئة 8 حروف سداسية عشرية
    clean_hex = ident.replace("-", "").lower()
    if len(clean_hex) >= 8 and re.fullmatch(r"[0-9a-f]+", clean_hex):
        try:
            res = supabase.table("tenants").select("*").execute()
            matches = [t for t in (res.data or []) if t.get("id", "").replace("-", "").lower().startswith(clean_hex)]
            if len(matches) == 1:
                return matches[0]
        except Exception:
            pass

    # 3. محاولة البحث بجدول merchants كاحتياطي بالـ ID فقط
    if len(clean_hex) >= 8 and re.fullmatch(r"[0-9a-f]+", clean_hex):
        try:
            res = supabase.table("merchants").select("*").eq("id", ident).execute()
            if res.data:
                return res.data[0]
        except Exception:
            pass

    return None


def get_tenant_dashboard_stats(tenant_id: str, tenant_row: dict = None) -> dict:
    """جلب إحصائيات سريعة للتاجر: حالة البوت، عدد الرسائل، المنتجات، والطلبات"""
    bot_enabled = bool(tenant_row.get("bot_enabled", True)) if tenant_row else True
    products_count = 0
    orders_count = 0
    messages_count = 0

    try:
        p_res = supabase.table("products").select("id", count="exact").eq("tenant_id", tenant_id).execute()
        products_count = p_res.count if p_res.count is not None else len(p_res.data or [])
    except Exception:
        pass

    try:
        o_res = supabase.table("orders").select("id", count="exact").eq("tenant_id", tenant_id).execute()
        orders_count = o_res.count if o_res.count is not None else len(o_res.data or [])
    except Exception:
        pass

    try:
        conv_res = supabase.table("conversations").select("id").eq("tenant_id", tenant_id).execute()
        conv_ids = [c["id"] for c in (conv_res.data or [])]
        if conv_ids:
            m_res = supabase.table("messages").select("id", count="exact").in_("conversation_id", conv_ids[:100]).execute()
            messages_count = m_res.count if m_res.count is not None else len(m_res.data or [])
    except Exception:
        pass

    return {
        "bot_enabled": bot_enabled,
        "products_count": products_count,
        "orders_count": orders_count,
        "messages_count": messages_count,
    }


def format_tenant_dashboard_data(tenant_row: dict) -> dict:
    tid = str(tenant_row.get("id"))
    stats = get_tenant_dashboard_stats(tid, tenant_row)

    # قراءة رابط Google Sheets
    sheets_url = tenant_row.get("google_sheets_url") or ""
    if not sheets_url:
        sheets_url = load_merchant_settings(tid).get("google_sheets_url", "")

    # جلب الفروع المسجلة للمتجر
    branches = []
    try:
        b_res = (
            supabase.table("branches")
            .select("id, name, governorate, city, address, google_maps_url, working_hours, phone")
            .eq("tenant_id", tid)
            .order("created_at", desc=False)
            .execute()
        )
        branches = b_res.data or []
    except Exception as e:
        print("Error fetching branches for dashboard:", e)

    # جلب أسعار الشحن
    try:
        s_res = (
            supabase.table("shipping_rates")
            .select("id,governorate,price")
            .eq("tenant_id", tid)
            .order("price", desc=False)
            .execute()
        )
        shipping_rates = s_res.data or []
    except Exception as e:
        print("Error fetching shipping rates for dashboard:", e)
        shipping_rates = []

    shipping_enabled = orders.is_shipping_enabled(tid)

    # تجهيز قنوات السوشيال ميديا
    fb_id = tenant_row.get("facebook_page_id") or ""
    fb_token = tenant_row.get("facebook_page_token") or ""
    ig_id = tenant_row.get("instagram_account_id") or ""
    wa_id = tenant_row.get("whatsapp_phone_number_id") or ""
    wa_token = tenant_row.get("whatsapp_access_token") or ""
    tt_id = tenant_row.get("tiktok_account_id") or ""
    tt_token = tenant_row.get("tiktok_access_token") or ""
    tg_id = tenant_row.get("telegram_chat_id") or ""
    bot_uname = os.environ.get("BOT_USERNAME", "ORIK_1_bot")

    # تحديد حالة القنوات بدقة:
    # متصل كامل: Page ID + Token
    # غير مكتمل: Page ID بدون Token
    if fb_id and fb_token:
        fb_status = "connected"
    elif fb_id and not fb_token:
        fb_status = "pending_token"
    else:
        fb_status = "disconnected"

    social_channels = {
        "facebook": {
            "name": "فيسبوك",
            "icon": "📘",
            "connected": fb_status == "connected",
            "status": fb_status,
            "id": fb_id,
            "url": f"https://facebook.com/{fb_id}" if fb_id else "",
        },
        "instagram": {
            "name": "إنستجرام",
            "icon": "📸",
            "connected": bool(ig_id),
            "status": "connected" if ig_id else "disconnected",
            "id": ig_id,
            "url": f"https://instagram.com/{ig_id}" if ig_id else "",
        },
        "whatsapp": {
            "name": "واتساب",
            "icon": "🟢",
            "connected": bool(wa_id and wa_token),
            "status": "connected" if (wa_id and wa_token) else ("pending_token" if wa_id else "disconnected"),
            "id": wa_id,
            "url": f"https://wa.me/{wa_id}" if wa_id else "",
        },
        "tiktok": {
            "name": "تيك توك",
            "icon": "🎵",
            "connected": bool(tt_id and tt_token),
            "status": "connected" if (tt_id and tt_token) else ("pending_token" if tt_id else "disconnected"),
            "id": tt_id,
            "url": f"https://tiktok.com/@{tt_id}" if tt_id else "",
        },
        "telegram": {
            "name": "تليجرام",
            "icon": "✈️",
            "connected": bool(tg_id),
            "status": "connected" if tg_id else "disconnected",
            "id": str(tg_id) if tg_id else "",
            "url": f"https://t.me/{bot_uname}",
        },
    }

    # جلب العروض والخصومات وكروت الهدايا
    try:
        p_res = (
            supabase.table("promotions")
            .select("*")
            .eq("tenant_id", tid)
            .order("created_at", desc=True)
            .execute()
        )
        promotions = p_res.data or []
    except Exception as e:
        print("Error fetching promotions for dashboard:", e)
        promotions = []

    return {
        "id": tenant_row.get("id"),
        "name": tenant_row.get("name"),
        "owner_phone": tenant_row.get("owner_phone") or "",
        "bot_enabled": bool(tenant_row.get("bot_enabled", True)),
        "facebook_page_id": fb_id,
        "facebook_page_token": tenant_row.get("facebook_page_token") or "",
        "instagram_account_id": ig_id,
        "whatsapp_phone_number_id": wa_id,
        "tiktok_account_id": tt_id,
        "google_sheets_url": sheets_url,
        "social_channels": social_channels,
        "branches": branches,
        "shipping_enabled": shipping_enabled,
        "shipping_rates": shipping_rates,
        "autopost_enabled": bool(tenant_row.get("autopost_enabled", False)),
        "autopost_interval_hours": int(tenant_row.get("autopost_interval_hours") or 24),
        "autopost_last_at": str(tenant_row.get("autopost_last_at") or ""),
        "promotions": promotions,
        "stats": stats,
    }


def get_dashboard_html() -> Optional[str]:
    candidates = [
        DASHBOARD_TEMPLATE_PATH,
        os.path.join(os.path.dirname(__file__), "templates", "dashboard.html"),
        os.path.join(os.getcwd(), "templates", "dashboard.html"),
        os.path.join(os.path.dirname(__file__), "templates/dashboard.html"),
        os.path.join(os.path.dirname(__file__), "templates\\dashboard.html"),
        os.path.join("/home/site/wwwroot", "templates", "dashboard.html"),
        os.path.join("/home/site/wwwroot", "templates/dashboard.html"),
        os.path.join("/home/site/wwwroot", "templates\\dashboard.html"),
        "templates/dashboard.html",
        "templates\\dashboard.html",
        "dashboard.html",
    ]
    for path in candidates:
        if path and os.path.exists(path) and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                print(f"Error reading template from {path}: {e}")
    return None


@app.get("/tiktokPzmur3aS6Y6aZHUVYotVZpsE5CekJpOU.txt")
async def tiktok_developer_verification_file():
    """توثيق ملكية الدومين والبادئة لتطبيق تيك توك"""
    fname = "tiktokPzmur3aS6Y6aZHUVYotVZpsE5CekJpOU.txt"
    if os.path.exists(fname):
        with open(fname, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content=content, media_type="text/plain")
    return Response(content="tiktokPzmur3aS6Y6aZHUVYotVZpsE5CekJpOU", media_type="text/plain")


@app.get("/privacy", response_class=HTMLResponse)
@app.get("/privacy-policy", response_class=HTMLResponse)
async def privacy_policy_page():
    return HTMLResponse("""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><title>سياسة الخصوصية | Privacy Policy</title>
<style>body{font-family:sans-serif;padding:40px;line-height:1.8;max-width:800px;margin:auto;color:#333;}</style>
</head>
<body>
<h1>سياسة الخصوصية (Privacy Policy)</h1>
<p>نحن نحترم خصوصيتك وملتزمون بحماية بيانات المستخدمين والعملاء. يقوم تطبيقنا بمعالجة الرسائل والتعليقات الواردة إلى منصات التواصل الاجتماعي (Facebook, Instagram, TikTok) لغرض الرد الآلي على استفسارات المنتجات وتأكيد وإدارة طلبات الشراء فقط.</p>
<h2>جمع البيانات واستخدامها</h2>
<p>لا نقوم ببيع أو مشاركة بياناتك مع أي أطراف ثالثة. يتم استخدام معرفات المستخدم والرسائل فقط لتقديم خدمة الرد الآلي وتأكيد الطلبات للتاجر.</p>
<h2>حذف البيانات (Data Deletion)</h2>
<p>يمكن لأي مستخدم طلب حذف سجل محادثاته وبياناته في أي وقت عبر مراسلة الحساب أو عبر التواصل مع الدعم الفني: apdoo0x0@gmail.com.</p>
<hr>
<h3>English Summary</h3>
<p>This application processes messages and comments from connected social platforms (Facebook, Instagram, TikTok) solely to provide automated customer support and order processing. We do not sell or share user data. To request data deletion, contact support at apdoo0x0@gmail.com.</p>
</body>
</html>""")


@app.get("/terms", response_class=HTMLResponse)
@app.get("/terms-of-service", response_class=HTMLResponse)
async def terms_page():
    return HTMLResponse("""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><title>شروط الاستخدام | Terms of Service</title>
<style>body{font-family:sans-serif;padding:40px;line-height:1.8;max-width:800px;margin:auto;color:#333;}</style>
</head>
<body>
<h1>شروط الاستخدام (Terms of Service)</h1>
<p>باستخدامك لخدمات المراسلة الآلية عبر صفحتنا، فإنك توافق على شروط الخدمة. الخدمة مقدمة لتسهيل الرد على الاستفسارات وتوفير تجربة تسوق سريعة ومريحة.</p>
</body>
</html>""")


# ===============================================================
# 🔐 Facebook Login for Business (OAuth One-Click Connection)
# ===============================================================

@app.get("/auth/facebook/login")
async def facebook_oauth_login(tenant_id: str, request: Request):
    """توجيه التاجر لصفحة فيسبوك الرسمية لمنح الصلاحيات بضغطة زر واحدة"""
    app_id = os.environ.get("META_APP_ID", "2549386585573918")
    base_url = os.environ.get("BASE_URL") or os.environ.get("DASHBOARD_URL") or str(request.base_url).rstrip("/")
    if base_url.startswith("http://") and "azurewebsites.net" in base_url:
        base_url = base_url.replace("http://", "https://", 1)
    redirect_uri = f"{base_url}/auth/facebook/callback"

    scope = "pages_show_list,pages_messaging,pages_read_engagement,pages_manage_engagement,pages_manage_posts,instagram_basic,instagram_manage_messages,instagram_manage_comments"
    oauth_url = (
        f"https://www.facebook.com/v21.0/dialog/oauth?"
        f"client_id={app_id}&redirect_uri={urllib.parse.quote(redirect_uri)}&state={tenant_id}&scope={scope}&response_type=code"
    )
    return RedirectResponse(oauth_url)


@app.get("/auth/facebook/callback")
async def facebook_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    """استقبال رمز فيسبوك وتوليد توكن دائم وربط الصفحة والويب هوك تلقائياً"""
    tenant_id = state
    base_url = os.environ.get("BASE_URL") or os.environ.get("DASHBOARD_URL") or str(request.base_url).rstrip("/")
    if base_url.startswith("http://") and "azurewebsites.net" in base_url:
        base_url = base_url.replace("http://", "https://", 1)

    if error or not code:
        err_msg = error_description or "تم إلغاء عملية الربط أو رفض الإذن من فيسبوك."
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head><meta charset="UTF-8"><title>فشل الربط</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f172a;color:#f87171;">
          <h2>❌ تعذر إكمال الربط مع فيسبوك</h2>
          <p style="color:#94a3b8;">{err_msg}</p>
          <a href="/dashboard?tenant_id={tenant_id or ''}" style="display:inline-block;margin-top:20px;padding:12px 24px;background:#4f46e5;color:white;border-radius:12px;text-decoration:none;font-weight:bold;">العودة للوحة التحكم</a>
        </body></html>
        """)

    tenant = resolve_tenant_for_dashboard(tenant_id)
    if not tenant:
        return HTMLResponse("<h3>المتجر غير موجود أو المعرّف غير صحيح</h3>", status_code=404)

    app_id = os.environ.get("META_APP_ID", "2549386585573918")
    app_secret = os.environ.get("META_APP_SECRET", "15576a7e986e654188f2f3ea0ce01a58")
    redirect_uri = f"{base_url}/auth/facebook/callback"

    async with httpx.AsyncClient() as client:
        # 1. استبدال code بـ User Access Token
        token_url = "https://graph.facebook.com/v21.0/oauth/access_token"
        token_params = {
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        resp = await client.get(token_url, params=token_params)
        token_data = resp.json()
        short_token = token_data.get("access_token")

        if not short_token:
            err = token_data.get("error", {}).get("message", "فشل استخراج التوكن من فيسبوك")
            return HTMLResponse(f"<h3 style='color:red;'>خطأ فيسبوك: {err}</h3>", status_code=400)

        # 2. ترقية التوكن إلى Long-lived User Token (صلاحية 60 يوماً ويولد Page Token دائم لا ينتهي أبداً)
        exchange_params = {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        }
        resp_long = await client.get(token_url, params=exchange_params)
        long_user_token = resp_long.json().get("access_token", short_token)

        # 3. جلب صفحات التاجر وحساب إنستجرام المرتبط
        accounts_url = "https://graph.facebook.com/v21.0/me/accounts"
        acc_params = {
            "access_token": long_user_token,
            "fields": "id,name,access_token,instagram_business_account",
        }
        acc_resp = await client.get(accounts_url, params=acc_params)
        acc_data = acc_resp.json()
        pages = acc_data.get("data", [])

        if not pages:
            return HTMLResponse("""
            <!DOCTYPE html>
            <html lang="ar" dir="rtl">
            <head><meta charset="UTF-8"><title>لا توجد صفحات</title></head>
            <body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f172a;color:#f8fafc;">
              <h2>⚠️ لم يتم العثور على أي صفحة فيسبوك تديرها</h2>
              <p style="color:#94a3b8;">تأكد من أنك تملك صلاحية الأدمن (Admin) على الصفحة المطلوبة، ومنحت تطبيقنا الصلاحية عند تسجيل الدخول.</p>
              <a href="/dashboard" style="display:inline-block;margin-top:20px;padding:12px 24px;background:#4f46e5;color:white;border-radius:12px;text-decoration:none;font-weight:bold;">العودة للوحة التحكم</a>
            </body></html>
            """)

        # نختار الصفحة الأولى تلقائياً أو التي تتطابق مع التاجر
        chosen_page = pages[0]
        page_id = str(chosen_page["id"])
        page_name = chosen_page.get("name", "الصفحة")
        page_token = chosen_page["access_token"]  # هذا التوكن دائم لا ينتهي أبداً Never-expiring
        ig_obj = chosen_page.get("instagram_business_account") or {}
        ig_id = str(ig_obj.get("id")) if ig_obj.get("id") else None

        # 4. الاشتراك التلقائي في الويب هوك (Auto-subscribe to Page Webhooks)
        try:
            sub_url = f"https://graph.facebook.com/v21.0/{page_id}/subscribed_apps"
            sub_payload = {
                "subscribed_fields": "messages,messaging_postbacks,feed,message_reads",
                "access_token": page_token,
            }
            await client.post(sub_url, data=sub_payload)
            print(f"✅ تم اشتراك صفحة «{page_name}» ({page_id}) في Webhooks تلقائياً!")
        except Exception as e:
            print(f"⚠️ تحذير: تعذر الاشتراك التلقائي بالويب هوك: {e}")

        # 5. تحديث بيانات التاجر في Supabase
        update_data = {
            "facebook_page_id": page_id,
            "facebook_page_token": page_token,
        }
        if ig_id:
            update_data["instagram_account_id"] = ig_id

        supabase.table("tenants").update(update_data).eq("id", tenant["id"]).execute()
        print(f"🎉 تم ربط صفحة «{page_name}» بالتاجر «{tenant['name']}» بضغطة زر بنجاح!")

        # 6. إعادة التوجيه للداشبورد مع إشعار نجاح
        dest = f"/dashboard?tenant_id={tenant['id']}&oauth_success=1&page_name={urllib.parse.quote(page_name)}"
        return RedirectResponse(dest)


# ===============================================================
# 🎵 TikTok OAuth One-Click Connection
# ===============================================================

@app.get("/auth/tiktok/login")
async def tiktok_oauth_login(tenant_id: str, request: Request):
    """توجيه التاجر لصفحة تيك توك الرسمية للربط بضغطة زر واحدة"""
    client_key = os.environ.get("TIKTOK_CLIENT_KEY") or os.environ.get("TIKTOK_APP_ID") or "aw5233agency"
    base_url = os.environ.get("BASE_URL") or os.environ.get("DASHBOARD_URL") or str(request.base_url).rstrip("/")
    if base_url.startswith("http://") and "azurewebsites.net" in base_url:
        base_url = base_url.replace("http://", "https://", 1)
    redirect_uri = f"{base_url}/auth/tiktok/callback"

    scope = "user.info.basic,video.list,comment.list,comment.list.manage"
    oauth_url = (
        f"https://www.tiktok.com/v2/auth/authorize/?"
        f"client_key={client_key}&scope={scope}&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}&state={tenant_id}"
    )
    return RedirectResponse(oauth_url)


@app.get("/auth/tiktok/callback")
async def tiktok_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    """استقبال رمز تيك توك وتوليد التوكن وحفظ الحساب في Supabase"""
    tenant_id = state
    base_url = os.environ.get("BASE_URL") or os.environ.get("DASHBOARD_URL") or str(request.base_url).rstrip("/")
    if base_url.startswith("http://") and "azurewebsites.net" in base_url:
        base_url = base_url.replace("http://", "https://", 1)

    if error or not code:
        err_msg = error_description or "تم إلغاء عملية الربط أو رفض الإذن من تيك توك."
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head><meta charset="UTF-8"><title>فشل الربط</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f172a;color:#f87171;">
          <h2>❌ تعذر إكمال الربط مع تيك توك</h2>
          <p style="color:#94a3b8;">{err_msg}</p>
          <a href="/dashboard?tenant_id={tenant_id or ''}" style="display:inline-block;margin-top:20px;padding:12px 24px;background:#4f46e5;color:white;border-radius:12px;text-decoration:none;font-weight:bold;">العودة للوحة التحكم</a>
        </body></html>
        """)

    tenant = resolve_tenant_for_dashboard(tenant_id)
    if not tenant:
        return HTMLResponse("<h3>المتجر غير موجود أو المعرّف غير صحيح</h3>", status_code=404)

    client_key = os.environ.get("TIKTOK_CLIENT_KEY") or os.environ.get("TIKTOK_APP_ID") or "aw5233agency"
    client_secret = os.environ.get("TIKTOK_APP_SECRET", "")
    redirect_uri = f"{base_url}/auth/tiktok/callback"

    async with httpx.AsyncClient() as client:
        token_url = "https://open.tiktokapis.com/v2/oauth/token/"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        try:
            resp = await client.post(token_url, data=data, headers=headers, timeout=12)
            res_json = resp.json()
            token_data = res_json.get("data") or res_json
            access_token = token_data.get("access_token")
            open_id = token_data.get("open_id") or token_data.get("creator_username") or "tiktok_channel"
        except Exception as e:
            return HTMLResponse(f"<h3 style='color:red;'>خطأ في الاتصال بخوادم تيك توك: {e}</h3>", status_code=500)

        if not access_token:
            err = res_json.get("error", {}).get("message") or res_json.get("message", "فشل استخراج التوكن من تيك توك")
            return HTMLResponse(f"<h3 style='color:red;'>خطأ تيك توك: {err}</h3>", status_code=400)

        # تحديث بيانات التاجر في Supabase
        supabase.table("tenants").update({
            "tiktok_account_id": str(open_id),
            "tiktok_access_token": str(access_token)
        }).eq("id", tenant["id"]).execute()

        dest = f"/dashboard?tenant_id={tenant['id']}&tiktok_success=1"
        return RedirectResponse(dest)


# ===============================================================
# 🟢 WhatsApp Cloud API One-Click OAuth & Quick Connect
# ===============================================================

@app.get("/auth/whatsapp/login")
async def whatsapp_oauth_login(tenant_id: str, request: Request):
    """توجيه التاجر لربط رقم واتساب التجاري بضغطة زر واحدة عبر Meta"""
    app_id = os.environ.get("META_APP_ID", "2549386585573918")
    base_url = os.environ.get("BASE_URL") or os.environ.get("DASHBOARD_URL") or str(request.base_url).rstrip("/")
    if base_url.startswith("http://") and "azurewebsites.net" in base_url:
        base_url = base_url.replace("http://", "https://", 1)
    redirect_uri = f"{base_url}/auth/whatsapp/callback"

    scope = "whatsapp_business_management,whatsapp_business_messaging"
    oauth_url = (
        f"https://www.facebook.com/v21.0/dialog/oauth?"
        f"client_id={app_id}&redirect_uri={urllib.parse.quote(redirect_uri)}&state={tenant_id}&scope={scope}&response_type=code"
    )
    return RedirectResponse(oauth_url)


@app.get("/auth/whatsapp/callback")
async def whatsapp_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    """استقبال رمز واتساب من ميتا واستخراج معرّف الهاتف والاشتراك بالويب هوك"""
    tenant_id = state
    base_url = os.environ.get("BASE_URL") or os.environ.get("DASHBOARD_URL") or str(request.base_url).rstrip("/")
    if base_url.startswith("http://") and "azurewebsites.net" in base_url:
        base_url = base_url.replace("http://", "https://", 1)

    if error or not code:
        err_msg = error_description or "تم إلغاء عملية الربط أو رفض الإذن من ميتا."
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head><meta charset="UTF-8"><title>فشل الربط</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f172a;color:#f87171;">
          <h2>❌ تعذر إكمال الربط مع واتساب</h2>
          <p style="color:#94a3b8;">{err_msg}</p>
          <a href="/dashboard?tenant_id={tenant_id or ''}" style="display:inline-block;margin-top:20px;padding:12px 24px;background:#4f46e5;color:white;border-radius:12px;text-decoration:none;font-weight:bold;">العودة للوحة التحكم</a>
        </body></html>
        """)

    tenant = resolve_tenant_for_dashboard(tenant_id)
    if not tenant:
        return HTMLResponse("<h3>المتجر غير موجود أو المعرّف غير صحيح</h3>", status_code=404)

    app_id = os.environ.get("META_APP_ID", "2549386585573918")
    app_secret = os.environ.get("META_APP_SECRET", "15576a7e986e654188f2f3ea0ce01a58")
    redirect_uri = f"{base_url}/auth/whatsapp/callback"

    async with httpx.AsyncClient() as client:
        token_url = "https://graph.facebook.com/v21.0/oauth/access_token"
        token_params = {
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        resp = await client.get(token_url, params=token_params)
        token_data = resp.json()
        short_token = token_data.get("access_token")

        if not short_token:
            err = token_data.get("error", {}).get("message", "فشل استخراج التوكن من ميتا")
            return HTMLResponse(f"<h3 style='color:red;'>خطأ ميتا: {err}</h3>", status_code=400)

        # ترقية التوكن لتوكن دائم
        exchange_params = {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        }
        resp_long = await client.get(token_url, params=exchange_params)
        long_token = resp_long.json().get("access_token", short_token)

        update_data = {
            "whatsapp_access_token": str(long_token)
        }

        # جلب WABA وأرقام الهواتف المرتبطة
        try:
            phone_url = "https://graph.facebook.com/v21.0/me?fields=phone_numbers"
            pr = await client.get(phone_url, params={"access_token": long_token})
            pdata = pr.json().get("phone_numbers", {}).get("data", [])
            if pdata:
                update_data["whatsapp_phone_number_id"] = str(pdata[0]["id"])
        except Exception as e:
            print("Error fetching WhatsApp phone number ID:", e)

        supabase.table("tenants").update(update_data).eq("id", tenant["id"]).execute()
        print(f"🎉 تم ربط واتساب بالتاجر «{tenant['name']}» بضغطة زر بنجاح!")

        dest = f"/dashboard?tenant_id={tenant['id']}&whatsapp_success=1"
        return RedirectResponse(dest)


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/dashboard/{tenant_id}", response_class=HTMLResponse)
async def serve_merchant_dashboard(tenant_id: Optional[str] = None):
    """عرض صفحة لوحة التحكم المتوافقة مع المتصفح و Telegram Web App مع حقن مسبق للبيانات"""
    content = get_dashboard_html()
    if not content:
        return HTMLResponse("<h3>قالب لوحة التحكم غير موجود</h3>", status_code=404)

    # 1. تحديد التاجر المستهدف
    tenant_obj = None
    if tenant_id:
        tenant_obj = resolve_tenant_for_dashboard(tenant_id)

    if not tenant_obj:
        try:
            res = supabase.table("tenants").select("*").eq("name", "ORIK").limit(1).execute()
            if res.data:
                tenant_obj = res.data[0]
            else:
                res = supabase.table("tenants").select("*").order("created_at", desc=False).limit(1).execute()
                if res.data:
                    tenant_obj = res.data[0]
        except Exception as e:
            print("Error finding fallback tenant:", e)

    # 2. حقن بيانات المتجر مسبقاً (SSR Injection) لظهور البيانات فوراً بدون أي تأخير
    if tenant_obj:
        try:
            formatted_data = format_tenant_dashboard_data(tenant_obj)
            data_json = json.dumps(formatted_data, ensure_ascii=False)
            injection = f"""<script>
window.__INITIAL_DATA__ = {data_json};
window.__DEFAULT_TENANT_ID__ = "{formatted_data['id']}";
</script>"""
            content = content.replace("</head>", f"{injection}\n</head>")
        except Exception as e:
            print("Error injecting initial dashboard data:", e)

    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    return HTMLResponse(content=content, headers=headers)


@app.get("/api/dashboard/{identifier}")
@app.get("/api/merchant/{identifier}")
@app.get("/api/merchants/{identifier}")
async def get_merchant_data_api(identifier: str):
    """جلب بيانات وإحصائيات التاجر عبر معرّفه"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")
    data = format_tenant_dashboard_data(tenant)
    return {"status": "success", "data": data}


@app.post("/api/dashboard/{identifier}")
@app.post("/api/merchant/{identifier}")
@app.post("/api/merchants/{identifier}")
async def update_merchant_data_api(identifier: str, payload: MerchantSettingsPayload):
    """تحديث بيانات التاجر في جدول merchants/tenants في Supabase وتخزين إعدادات الشيت بأمان"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")

    tid = tenant["id"]
    updates = {}
    if payload.bot_enabled is not None:
        updates["bot_enabled"] = payload.bot_enabled
    if payload.facebook_page_id is not None:
        updates["facebook_page_id"] = payload.facebook_page_id.strip() or None
    if payload.facebook_page_token is not None:
        updates["facebook_page_token"] = payload.facebook_page_token.strip() or None

    # 1. تحديث جدول التجار في Supabase
    if updates:
        updated_db = False
        try:
            supabase.table("tenants").update(updates).eq("id", tid).execute()
            updated_db = True
        except Exception:
            pass

        if not updated_db:
            try:
                supabase.table("merchants").update(updates).eq("id", tid).execute()
                updated_db = True
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"فشل تحديث البيانات في قاعدة البيانات: {e}")

    # 2. حفظ رابط Google Sheets
    if payload.google_sheets_url is not None:
        sheets_val = payload.google_sheets_url.strip()
        try:
            supabase.table("tenants").update({"google_sheets_url": sheets_val or None}).eq("id", tid).execute()
        except Exception:
            try:
                supabase.table("merchants").update({"google_sheets_url": sheets_val or None}).eq("id", tid).execute()
            except Exception:
                pass
        save_merchant_settings(tid, {"google_sheets_url": sheets_val})

    updated_tenant = resolve_tenant_for_dashboard(tid) or tenant
    data = format_tenant_dashboard_data(updated_tenant)
    return {
        "status": "success",
        "message": "تم حفظ الإعدادات بنجاح",
        "data": data,
    }


@app.post("/api/dashboard/{identifier}/toggle-bot")
@app.post("/api/merchant/{identifier}/toggle-bot")
async def toggle_merchant_bot_api(identifier: str, payload: Optional[ToggleBotPayload] = None):
    """تشغيل أو إيقاف الرد التلقائي للبوت مؤقتاً"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")

    tid = tenant["id"]
    current_status = bool(tenant.get("bot_enabled", True))
    new_status = not current_status if (payload is None or payload.bot_enabled is None) else bool(payload.bot_enabled)

    updated_db = False
    try:
        supabase.table("tenants").update({"bot_enabled": new_status}).eq("id", tid).execute()
        updated_db = True
    except Exception:
        pass

    if not updated_db:
        try:
            supabase.table("merchants").update({"bot_enabled": new_status}).eq("id", tid).execute()
            updated_db = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"فشل تعديل حالة البوت: {e}")

    return {
        "status": "success",
        "bot_enabled": new_status,
        "message": f"تم {'تفعيل' if new_status else 'إيقاف'} الرد التلقائي بنجاح",
    }


@app.post("/api/dashboard/{identifier}/branches")
@app.post("/api/merchant/{identifier}/branches")
async def add_branch_api(identifier: str, payload: BranchPayload):
    """إضافة فرع جديد للمتجر من لوحة التحكم"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")
    tid = tenant["id"]
    try:
        data = {
            "tenant_id": tid,
            "name": payload.name.strip(),
            "governorate": payload.governorate.strip(),
            "address": payload.address.strip(),
            "google_maps_url": (payload.google_maps_url or "").strip(),
            "working_hours": (payload.working_hours or "من 12 ظهراً لـ 11 مساءً").strip(),
            "phone": (payload.phone or "").strip(),
        }
        res = supabase.table("branches").insert(data).execute()
        return {"status": "success", "message": "تم إضافة الفرع بنجاح", "branch": res.data[0] if res.data else {}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل إضافة الفرع: {e}")


@app.delete("/api/dashboard/{identifier}/branches/{branch_id}")
@app.delete("/api/merchant/{identifier}/branches/{branch_id}")
async def delete_branch_api(identifier: str, branch_id: str):
    """حذف فرع المتجر من لوحة التحكم"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")
    tid = tenant["id"]
    try:
        supabase.table("branches").delete().eq("id", branch_id).eq("tenant_id", tid).execute()
        return {"status": "success", "message": "تم حذف الفرع بنجاح"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل حذف الفرع: {e}")


@app.post("/api/dashboard/{identifier}/shipping-rates")
@app.post("/api/merchant/{identifier}/shipping-rates")
async def save_shipping_rate_api(identifier: str, payload: ShippingRatePayload):
    """إضافة أو تحديث سعر شحن لمحافظة معينة أو شحن موحد لجميع المحافظات"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")
    tid = tenant["id"]

    try:
        price = float(payload.price)
        if price < 0:
            raise HTTPException(status_code=400, detail="سعر الشحن لا يمكن أن يكون سالباً")

        gov = (payload.governorate or "جميع المحافظات").strip()
        is_flat = payload.is_flat or gov in ("جميع المحافظات", "شحن موحد", "الكل", "كل المحافظات")

        if is_flat:
            # مسح أي تسعيرة قديمة باسم "جميع المحافظات" وتثبيت السعر الموحد
            supabase.table("shipping_rates").delete().eq("tenant_id", tid).eq("governorate", "جميع المحافظات").execute()
            res = supabase.table("shipping_rates").insert({"tenant_id": tid, "governorate": "جميع المحافظات", "price": price}).execute()
            return {"status": "success", "message": f"تم تطبيق الشحن الموحد ({price:g} ج.م) لجميع المحافظات بنجاح!", "data": res.data}
        else:
            # تسعيرة لمحافظة معينة: تحديث إذا كانت موجودة أو إضافة جديدة
            existing = supabase.table("shipping_rates").select("id").eq("tenant_id", tid).eq("governorate", gov).execute().data
            if existing:
                res = supabase.table("shipping_rates").update({"price": price}).eq("id", existing[0]["id"]).execute()
            else:
                res = supabase.table("shipping_rates").insert({"tenant_id": tid, "governorate": gov, "price": price}).execute()
            return {"status": "success", "message": f"تم تحديث سعر شحن {gov} إلى {price:g} ج.م بنجاح!", "data": res.data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل حفظ سعر الشحن: {e}")


@app.delete("/api/dashboard/{identifier}/shipping-rates/{rate_id}")
@app.delete("/api/merchant/{identifier}/shipping-rates/{rate_id}")
async def delete_shipping_rate_api(identifier: str, rate_id: str):
    """حذف تسعيرة شحن محافظة من لوحة التحكم"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")
    tid = tenant["id"]
    try:
        supabase.table("shipping_rates").delete().eq("id", rate_id).eq("tenant_id", tid).execute()
        return {"status": "success", "message": "تم حذف تسعيرة الشحن بنجاح"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل حذف تسعيرة الشحن: {e}")


@app.post("/api/dashboard/{identifier}/toggle-shipping")
@app.post("/api/merchant/{identifier}/toggle-shipping")
async def toggle_shipping_api(identifier: str, payload: Optional[ToggleShippingPayload] = None):
    """تفعيل أو إيقاف الشحن للمتجر"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")
    tid = tenant["id"]

    try:
        current_status = orders.is_shipping_enabled(tid, fresh=True)
        new_status = not current_status if (payload is None or payload.shipping_enabled is None) else payload.shipping_enabled
        ok, err = orders.set_shipping_enabled(tid, new_status)
        if not ok:
            raise HTTPException(status_code=500, detail=err or "فشل تحديث حالة الشحن")
        return {"status": "success", "shipping_enabled": new_status, "message": f"تم {'تفعيل' if new_status else 'إيقاف'} الشحن بنجاح"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"خطأ أثناء تحديث حالة الشحن: {e}")


@app.post("/api/dashboard/{identifier}/autopost-trigger")
@app.post("/api/merchant/{identifier}/autopost-trigger")
async def autopost_trigger_api(identifier: str):
    """نشر بوست إعلاني فوراً لأحد منتجات المخزن على صفحة فيسبوك"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")
    res = await trigger_autopost_for_tenant(tenant["id"])
    if res.get("status") != "success":
        raise HTTPException(status_code=400, detail=res.get("message", "فشل النشر"))
    return res


@app.post("/api/dashboard/{identifier}/autopost-toggle")
@app.post("/api/merchant/{identifier}/autopost-toggle")
async def autopost_toggle_api(identifier: str, payload: ToggleAutopostPayload):
    """تفعيل أو إيقاف النشر التلقائي وتحديد الفترة بالساعات"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود أو المعرّف غير صحيح")
    tid = tenant["id"]
    updates = {}
    if payload.autopost_enabled is not None:
        updates["autopost_enabled"] = bool(payload.autopost_enabled)
    if payload.autopost_interval_hours is not None:
        updates["autopost_interval_hours"] = max(1, int(payload.autopost_interval_hours))
    try:
        supabase.table("tenants").update(updates).eq("id", tid).execute()
        return {"status": "success", "message": "تم تحديث إعدادات النشر التلقائي بنجاح", **updates}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل تحديث إعدادات النشر: {e}")


@app.get("/api/dashboard/{identifier}/promotions")
@app.get("/api/merchant/{identifier}/promotions")
async def get_promotions_api(identifier: str):
    """عرض قائمة العروض والخصومات والفاوتشرات الخاصة بالمتجر"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود")
    tid = tenant["id"]
    try:
        res = supabase.table("promotions").select("*").eq("tenant_id", tid).order("created_at", desc=True).execute()
        return {"status": "success", "data": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل جلب العروض: {e}")


@app.post("/api/dashboard/{identifier}/promotions")
@app.post("/api/merchant/{identifier}/promotions")
async def add_promotion_api(identifier: str, payload: PromotionPayload):
    """إضافة عرض ترويجي جديد (شحن مجاني، نسبة خصم، مبلغ ثابت، أو كارت هدايا/فاوتشر)"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود")
    tid = tenant["id"]
    if not payload.description.strip():
        raise HTTPException(status_code=400, detail="يرجى كتابة وصف واضح للعرض")

    insert_data = {
        "tenant_id": tid,
        "code": payload.code.strip().upper() if payload.code else None,
        "promo_type": payload.promo_type.strip().lower(),
        "discount_value": float(payload.discount_value or 0),
        "min_order_items": int(payload.min_order_items or 1),
        "description": payload.description.strip(),
        "is_active": True,
    }

    if payload.min_order_value and payload.min_order_value > 0:
        insert_data["min_order_value"] = float(payload.min_order_value)

    if payload.target_items and payload.target_items.strip():
        insert_data["target_items"] = payload.target_items.strip()

    if payload.branch and payload.branch.strip() and payload.branch.strip() != "كل الفروع":
        insert_data["branch"] = payload.branch.strip()

    try:
        res = supabase.table("promotions").insert(insert_data).execute()
        return {"status": "success", "message": "تمت إضافة العرض الترويجي بنجاح! 🎁", "promo": res.data[0] if res.data else {}}
    except Exception as e:
        err_str = str(e)
        if "branch" in err_str:
            b_val = insert_data.pop("branch", None)
            if b_val and f"[الفرع: {b_val}]" not in insert_data["description"]:
                insert_data["description"] = f"[الفرع: {b_val}] {insert_data['description']}"
            try:
                res = supabase.table("promotions").insert(insert_data).execute()
                return {"status": "success", "message": "تمت إضافة العرض الترويجي بنجاح! 🎁", "promo": res.data[0] if res.data else {}}
            except Exception as e_br:
                err_str = str(e_br)

        if "target_items" in err_str:
            insert_data.pop("target_items", None)
            try:
                res = supabase.table("promotions").insert(insert_data).execute()
                return {"status": "success", "message": "تمت إضافة العرض الترويجي بنجاح! 🎁", "promo": res.data[0] if res.data else {}}
            except Exception as e_ti:
                err_str = str(e_ti)

        if "promotions_type_check" in err_str:
            insert_data["promo_type"] = "fixed"
            if "[كارت هدايا]" not in insert_data["description"] and "كارت" not in insert_data["description"]:
                insert_data["description"] = f"[كارت هدايا / فاوتشر] {insert_data['description']}"
            try:
                res = supabase.table("promotions").insert(insert_data).execute()
                return {"status": "success", "message": "تمت إضافة العرض الترويجي بنجاح! 🎁", "promo": res.data[0] if res.data else {}}
            except Exception as e_retry:
                err_str = str(e_retry)

        if "min_order_value" in err_str:
            insert_data.pop("min_order_value", None)
            try:
                res = supabase.table("promotions").insert(insert_data).execute()
                return {"status": "success", "message": "تمت إضافة العرض الترويجي بنجاح! 🎁", "promo": res.data[0] if res.data else {}}
            except Exception as e2:
                raise HTTPException(status_code=500, detail=f"فشل إضافة العرض: {e2}")
        raise HTTPException(status_code=500, detail=f"فشل إضافة العرض الترويجي: {e}")


@app.delete("/api/dashboard/{identifier}/promotions/{promo_id}")
@app.delete("/api/merchant/{identifier}/promotions/{promo_id}")
async def delete_promotion_api(identifier: str, promo_id: str):
    """حذف أو إيقاف عرض ترويجي"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود")
    tid = tenant["id"]
    try:
        supabase.table("promotions").delete().eq("id", promo_id).eq("tenant_id", tid).execute()
        return {"status": "success", "message": "تم حذف العرض الترويجي بنجاح"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل حذف العرض: {e}")


# =====================================================================
# إدارة منتجات وصور المخزن (Products & Images)
# =====================================================================

@app.get("/api/dashboard/{identifier}/products")
@app.get("/api/merchant/{identifier}/products")
async def get_dashboard_products_api(identifier: str):
    """جلب قائمة منتجات المتجر مع حساب إجمالي المخزون وحالة الصورة"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود")
    tid = str(tenant["id"])

    try:
        prods_res = (
            supabase.table("products")
            .select("id, name, price, material, image_url, category, active, created_at")
            .eq("tenant_id", tid)
            .order("name", desc=False)
            .execute()
        )
        prods = prods_res.data or []

        vars_res = (
            supabase.table("variants")
            .select("id, product_id, sku, size, color, available, stock, reserved, active")
            .eq("tenant_id", tid)
            .eq("active", True)
            .execute()
        )
        vars_data = vars_res.data or []
        stock_by_prod = {}
        variants_by_prod = {}
        vars_list_by_prod = {}
        for v in vars_data:
            pid = v.get("product_id")
            avail = v.get("available")
            if avail is None:
                avail = (v.get("stock") or 0) - (v.get("reserved") or 0)
            avail_int = max(0, int(avail or 0))
            stock_by_prod[pid] = stock_by_prod.get(pid, 0) + avail_int
            variants_by_prod[pid] = variants_by_prod.get(pid, 0) + 1
            if pid not in vars_list_by_prod:
                vars_list_by_prod[pid] = []
            vars_list_by_prod[pid].append({
                "id": v.get("id"),
                "sku": v.get("sku"),
                "size": v.get("size"),
                "color": v.get("color"),
                "stock": v.get("stock") or 0,
                "available": avail_int,
            })

        for p in prods:
            pid = p["id"]
            p["total_stock"] = stock_by_prod.get(pid, 0)
            p["variants_count"] = variants_by_prod.get(pid, 0)
            p["variants"] = vars_list_by_prod.get(pid, [])

        return {"status": "success", "products": prods}
    except Exception as e:
        print("Error fetching products for dashboard:", e)
        raise HTTPException(status_code=500, detail=f"فشل جلب المنتجات: {e}")


@app.post("/api/dashboard/{identifier}/products/{product_id}/image")
@app.post("/api/merchant/{identifier}/products/{product_id}/image")
async def update_product_image_api(identifier: str, product_id: str, payload: ProductImagePayload):
    """رفع أو تحديث صورة المنتج وضغطها بذكاء لتسريع التحميل للعملاء والرد التلقائي"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود")
    tid = str(tenant["id"])

    p_check = (
        supabase.table("products")
        .select("id, name")
        .eq("tenant_id", tid)
        .eq("id", product_id)
        .execute()
    )
    if not p_check.data:
        raise HTTPException(status_code=404, detail="المنتج غير موجود في متجرك")
    prod_name = p_check.data[0]["name"]

    target_url = None
    if payload.image_data and payload.image_data.strip():
        raw_str = payload.image_data.strip()
        mime = "image/jpeg"
        if "," in raw_str:
            header, encoded = raw_str.split(",", 1)
            if "data:" in header and ";" in header:
                mime = header.split("data:")[1].split(";")[0]
        else:
            encoded = raw_str

        try:
            img_bytes = base64.b64decode(encoded)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"بيانات الصورة غير صالحة ({e})")

        try:
            target_url = upload_product_image_to_storage(
                supabase, tid, img_bytes, mime=mime
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"فشل رفع الصورة: {e}")

    elif payload.image_url and payload.image_url.strip():
        clean_url = payload.image_url.strip()
        if not clean_url.startswith("http://") and not clean_url.startswith("https://"):
            raise HTTPException(status_code=400, detail="رابط الصورة يجب أن يبدأ بـ http أو https")
        target_url = clean_url
    else:
        raise HTTPException(status_code=400, detail="يرجى إرسال ملف الصورة أو رابطها")

    try:
        supabase.table("products").update({"image_url": target_url}).eq("id", product_id).eq("tenant_id", tid).execute()
        supabase.table("variants").update({"image_url": target_url}).eq("product_id", product_id).eq("tenant_id", tid).execute()
        return {
            "status": "success",
            "message": f"تم تحديث صورة «{prod_name}» بنجاح! 📸",
            "image_url": target_url,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل تحديث قاعدة البيانات: {e}")


@app.delete("/api/dashboard/{identifier}/products/{product_id}/image")
@app.delete("/api/merchant/{identifier}/products/{product_id}/image")
async def delete_product_image_api(identifier: str, product_id: str):
    """إزالة صورة المنتج لو تم إرسالها بالخطأ أو غير مناسبة"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود")
    tid = str(tenant["id"])

    p_check = (
        supabase.table("products")
        .select("id, name")
        .eq("tenant_id", tid)
        .eq("id", product_id)
        .execute()
    )
    if not p_check.data:
        raise HTTPException(status_code=404, detail="المنتج غير موجود")
    prod_name = p_check.data[0]["name"]

    try:
        supabase.table("products").update({"image_url": None}).eq("id", product_id).eq("tenant_id", tid).execute()
        supabase.table("variants").update({"image_url": None}).eq("product_id", product_id).eq("tenant_id", tid).execute()
        return {
            "status": "success",
            "message": f"تمت إزالة صورة «{prod_name}» بنجاح! 🗑️",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل حذف الصورة: {e}")


def _norm_ar_srv(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"[أإآ]", "ا", s)
    s = re.sub(r"ة", "ه", s)
    s = re.sub(r"ى", "ي", s)
    return s.lower()


def _generate_sku_srv() -> str:
    """توليد كود صنف بحروف لاتينية فقط (P + 6 حروف عشوائية)"""
    letters = "".join(secrets.choice(string.ascii_uppercase) for _ in range(6))
    return f"P{letters}"


@app.post("/api/dashboard/{identifier}/stock/add")
@app.post("/api/merchant/{identifier}/stock/add")
async def add_stock_api(identifier: str, payload: StockAddPayload):
    """إضافة قطع جديدة للمخزون أو تسجيل صنف جديد بكامل تفاصيله (فئة، خامة، صورة)"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود")
    tid = str(tenant["id"])

    if payload.qty <= 0:
        raise HTTPException(status_code=400, detail="الكمية المضافة يجب أن تكون 1 أو أكثر")

    size = payload.size.strip().upper()
    color = payload.color.strip()
    if not size or not color:
        raise HTTPException(status_code=400, detail="المقاس واللون مطلوبان لتحديد القطع المضافة بدقة")

    # معالجة الصورة في حال تم إرسالها base64
    uploaded_image_url = None
    if payload.image_data and payload.image_data.strip():
        raw_str = payload.image_data.strip()
        mime = "image/jpeg"
        if "," in raw_str:
            header, encoded = raw_str.split(",", 1)
            if "data:" in header and ";" in header:
                mime = header.split("data:")[1].split(";")[0]
        else:
            encoded = raw_str

        try:
            img_bytes = base64.b64decode(encoded)
            uploaded_image_url = upload_product_image_to_storage(
                supabase,
                tid,
                img_bytes,
                filename=f"stock_{(payload.product_name or 'prod')[:15]}",
                mime=mime,
            )
        except Exception as e:
            print(f"Warning: Failed to optimize/upload image during stock add: {e}")

    product_id = payload.product_id
    prod_name = payload.product_name or ""

    # 1. إذا تم تحديد product_id:
    if product_id:
        p_check = (
            supabase.table("products")
            .select("id, name, price, material, category, image_url")
            .eq("tenant_id", tid)
            .eq("id", product_id)
            .execute()
        )
        if not p_check.data:
            raise HTTPException(status_code=404, detail="المنتج غير موجود")
        current_p = p_check.data[0]
        prod_name = current_p["name"]

        # تحديث بيانات المنتج إذا توافرت خامة أو فئة أو صورة
        prod_updates = {}
        if uploaded_image_url:
            prod_updates["image_url"] = uploaded_image_url
        if payload.material and payload.material.strip() and not current_p.get("material"):
            prod_updates["material"] = payload.material.strip()
        if payload.category and payload.category.strip() and not current_p.get("category"):
            prod_updates["category"] = payload.category.strip()
        if payload.price and payload.price > 0 and float(current_p.get("price") or 0) <= 0:
            prod_updates["price"] = float(payload.price)
        if prod_updates:
            try:
                supabase.table("products").update(prod_updates).eq("id", product_id).execute()
            except Exception as e:
                print("Warning updating product:", e)

    else:
        # 2. إذا تم تحديد اسم المنتج (إضافة صنف جديد أو البحث بالاسم):
        if not prod_name.strip():
            raise HTTPException(status_code=400, detail="يرجى كتابة اسم الصنف أو اختيار منتج موجود")

        prod_norm = _norm_ar_srv(prod_name)
        existing_prods = (
            supabase.table("products")
            .select("id, name, price, material, category, image_url")
            .eq("tenant_id", tid)
            .execute()
            .data or []
        )
        matched = None
        for ep in existing_prods:
            if _norm_ar_srv(ep.get("name", "")) == prod_norm:
                matched = ep
                break

        if matched:
            product_id = matched["id"]
            prod_name = matched["name"]
            prod_updates = {}
            if uploaded_image_url and not matched.get("image_url"):
                prod_updates["image_url"] = uploaded_image_url
            if payload.material and payload.material.strip() and not matched.get("material"):
                prod_updates["material"] = payload.material.strip()
            if payload.category and payload.category.strip() and not matched.get("category"):
                prod_updates["category"] = payload.category.strip()
            if prod_updates:
                try:
                    supabase.table("products").update(prod_updates).eq("id", product_id).execute()
                except Exception as e:
                    print("Warning updating matched product:", e)
        else:
            # إنشاء منتج جديد في قاعدة البيانات
            new_prod_data = {
                "tenant_id": tid,
                "name": prod_name.strip(),
                "price": float(payload.price or 0),
                "category": (payload.category or "رجالي").strip(),
                "material": (payload.material or "").strip(),
                "active": True,
                **({"image_url": uploaded_image_url} if uploaded_image_url else {}),
            }
            try:
                new_p_res = supabase.table("products").insert(new_prod_data).execute()
                if not new_p_res.data:
                    raise RuntimeError("لم يتم إرجاع بيانات المنتج بعد إنشائه")
                product_id = new_p_res.data[0]["id"]
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"فشل إنشاء المنتج: {e}")

    # 3. فحص أو إنشاء الـ Variant وتحديث الكميات:
    var_rows = (
        supabase.table("variants")
        .select("id, sku, size, color, stock, reserved, available")
        .eq("tenant_id", tid)
        .eq("product_id", product_id)
        .execute()
        .data or []
    )
    target_color_norm = _norm_ar_srv(color)
    matched_var = None
    for vr in var_rows:
        if vr.get("size", "").strip().upper() == size and _norm_ar_srv(vr.get("color", "")) == target_color_norm:
            matched_var = vr
            break

    if matched_var:
        current_stock = int(matched_var.get("stock") or 0)
        reserved = int(matched_var.get("reserved") or 0)
        new_stock = current_stock + payload.qty
        new_avail = max(0, new_stock - reserved)
        var_updates = {"stock": new_stock, "available": new_avail}
        if uploaded_image_url and not matched_var.get("image_url"):
            var_updates["image_url"] = uploaded_image_url
        try:
            supabase.table("variants").update(var_updates).eq("id", matched_var["id"]).execute()
            sku = matched_var.get("sku", "")
            return {
                "status": "success",
                "message": f"تمت إضافة {payload.qty} قطعة إلى «{prod_name}» ({color} - {size})! المخزون الحالي: {new_stock} قطعة 📦",
                "product_id": product_id,
                "product_name": prod_name,
                "variant_id": matched_var["id"],
                "sku": sku,
                "new_stock": new_stock,
                "available": new_avail,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"فشل تحديث مخزون الصنف: {e}")
    else:
        new_sku = _generate_sku_srv()
        new_var_data = {
            "tenant_id": tid,
            "product_id": product_id,
            "sku": new_sku,
            "size": size,
            "color": color,
            "stock": payload.qty,
            "available": payload.qty,
            "reserved": 0,
            "active": True,
            **({"image_url": uploaded_image_url} if uploaded_image_url else {}),
        }
        try:
            new_v_res = supabase.table("variants").insert(new_var_data).execute()
            return {
                "status": "success",
                "message": f"تم تسجيل صنف جديد بنجاح: «{prod_name}» ({color} - {size}) برصيد {payload.qty} قطعة! 📦",
                "product_id": product_id,
                "product_name": prod_name,
                "variant_id": new_v_res.data[0]["id"] if new_v_res.data else None,
                "sku": new_sku,
                "new_stock": payload.qty,
                "available": payload.qty,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"فشل إضافة صنف المخزون: {e}")


# =====================================================================
# إدارة ومتابعة الطلبات (Orders Management)
# =====================================================================

@app.get("/api/dashboard/{identifier}/orders")
@app.get("/api/merchant/{identifier}/orders")
async def get_dashboard_orders_api(identifier: str):
    """جلب قائمة الطلبات مع تفاصيل الأصناف المحسوبة لكل أوردر وتلخيص المطلوب"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود")
    tid = str(tenant["id"])

    try:
        o_res = (
            supabase.table("orders")
            .select("*")
            .eq("tenant_id", tid)
            .order("order_number", desc=True)
            .limit(100)
            .execute()
        )
        orders_list = o_res.data or []
        if not orders_list:
            return {"status": "success", "orders": []}

        order_ids = [o["id"] for o in orders_list]

        items_res = (
            supabase.table("order_items")
            .select("id, order_id, variant_id, qty, unit_price, variants(sku, size, color, image_url, products(name, image_url, price))")
            .in_("order_id", order_ids)
            .execute()
        )
        items_data = items_res.data or []

        items_by_order = {}
        for it in items_data:
            oid = it["order_id"]
            var = it.get("variants") or {}
            prod = var.get("products") or {}
            pname = prod.get("name") or "صنف"
            color = var.get("color") or ""
            size = var.get("size") or ""
            img = var.get("image_url") or prod.get("image_url") or ""
            qty = int(it.get("qty") or 1)
            uprice = float(it.get("unit_price") or 0)

            item_obj = {
                "id": it["id"],
                "name": pname,
                "color": color,
                "size": size,
                "sku": var.get("sku") or "",
                "qty": qty,
                "unit_price": uprice,
                "subtotal": round(qty * uprice, 2),
                "image_url": img,
            }
            items_by_order.setdefault(oid, []).append(item_obj)

        for o in orders_list:
            oid = o["id"]
            o_items = items_by_order.get(oid, [])
            o["items"] = o_items

            if o_items:
                summary_parts = []
                for it in o_items:
                    detail = f"{it['name']}"
                    specs = []
                    if it['color']: specs.append(it['color'])
                    if it['size']: specs.append(it['size'])
                    if specs:
                        detail += f" ({' - '.join(specs)})"
                    detail += f" × {it['qty']}"
                    summary_parts.append(detail)
                o["items_summary"] = " + ".join(summary_parts)
            else:
                o["items_summary"] = "طلب بدون أصناف مسجلة"

        return {"status": "success", "orders": orders_list}
    except Exception as e:
        print("Error fetching orders for dashboard:", e)
        raise HTTPException(status_code=500, detail=f"فشل جلب الطلبات: {e}")


@app.patch("/api/dashboard/{identifier}/orders/{order_id}/status")
@app.patch("/api/merchant/{identifier}/orders/{order_id}/status")
async def update_order_status_api(identifier: str, order_id: str, payload: OrderStatusPayload):
    """تحديث حالة الأوردر (اتشحن، تم البيع والتسليم، قيد التجهيز، أو ملغي)"""
    tenant = resolve_tenant_for_dashboard(identifier)
    if not tenant:
        raise HTTPException(status_code=404, detail="المتجر غير موجود")
    tid = str(tenant["id"])

    new_status = payload.status.strip().lower()
    allowed_statuses = {
        "shipped": "تم الشحن 🚚",
        "delivered": "تم البيع والتسليم ✅",
        "confirmed": "مؤكد / قيد التجهيز 📦",
        "new": "طلب جديد 🆕",
        "cancelled": "ملغي ❌",
    }
    if new_status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"الحالة غير صالحة. الحالات المتاحة: {', '.join(allowed_statuses.keys())}"
        )

    o_res = (
        supabase.table("orders")
        .select("id, order_number, status, phone, customer_name")
        .eq("tenant_id", tid)
        .eq("id", order_id)
        .execute()
    )
    if not o_res.data:
        raise HTTPException(status_code=404, detail="الأوردر غير موجود")
    order_row = o_res.data[0]
    onum = order_row["order_number"]

    # لو الإلغاء: نستخدم دالة cancel_order لاسترجاع البضاعة للمخزن بأمان
    if new_status == "cancelled":
        res = orders.cancel_order(tid, onum, reason=payload.reason or "إلغاء من لوحة التحكم")
        if res.get("status") != "ok":
            raise HTTPException(status_code=400, detail=res.get("reason", "فشل إلغاء الأوردر"))
        return {
            "status": "success",
            "message": f"تم إلغاء الأوردر #{onum} وإرجاع بضاعته للمخزن ❌",
            "order_status": "cancelled",
        }

    try:
        supabase.table("orders").update({"status": new_status}).eq("id", order_id).eq("tenant_id", tid).execute()
        return {
            "status": "success",
            "message": f"تم تحديث حالة الأوردر #{onum} إلى: {allowed_statuses[new_status]}",
            "order_status": new_status,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل تحديث حالة الأوردر: {e}")


@app.get("/api/dashboard/default")
@app.get("/api/merchant/default")
async def get_default_dashboard_merchant():
    """جلب المتجر الافتراضي للوحة التحكم (مثل متجر ORIK)"""
    try:
        res = supabase.table("tenants").select("*").eq("name", "ORIK").limit(1).execute()
        if not res.data:
            res = supabase.table("tenants").select("*").order("created_at", desc=False).limit(1).execute()
        if res.data:
            data = format_tenant_dashboard_data(res.data[0])
            return {"status": "success", "data": data}
    except Exception as e:
        print("Error fetching default merchant:", e)
    raise HTTPException(status_code=404, detail="لم يتم العثور على أي متجر مسجل")


@app.get("/api/dashboard/by-telegram/{chat_id}")
@app.get("/api/merchant/by-telegram/{chat_id}")
async def get_merchant_by_telegram_chat_id(chat_id: str):
    """التحقق التلقائي من التاجر عبر معرّف Telegram Web App"""
    cid = str(chat_id).strip()
    try:
        res = supabase.table("tenants").select("*").eq("telegram_chat_id", cid).execute()
        if res.data:
            data = format_tenant_dashboard_data(res.data[0])
            return {"status": "success", "data": data}
    except Exception:
        pass

    try:
        if cid.isdigit():
            res = supabase.table("tenants").select("*").eq("telegram_chat_id", int(cid)).execute()
            if res.data:
                data = format_tenant_dashboard_data(res.data[0])
                return {"status": "success", "data": data}
    except Exception:
        pass

    # إذا لم يُعثر على المتجر (مثل السوبر أدمن أو تجربة البوت)، نرجع المتجر الافتراضي ORIK
    try:
        res = supabase.table("tenants").select("*").eq("name", "ORIK").limit(1).execute()
        if not res.data:
            res = supabase.table("tenants").select("*").order("created_at", desc=False).limit(1).execute()
        if res.data:
            data = format_tenant_dashboard_data(res.data[0])
            return {"status": "success", "data": data}
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="لم يتم العثور على متجر مرتبط برقم التيليجرام هذا")


@app.get("/api/merchants")
async def list_merchants_summary_api():
    """قائمة سريعة بأسماء ومعرّفات المتاجر للاختيار"""
    try:
        res = supabase.table("tenants").select("id, name, bot_enabled").order("name").execute()
        return {"status": "success", "data": res.data or []}
    except Exception as e:
        return {"status": "error", "data": [], "detail": str(e)}


@app.post("/api/maintenance/purge-old-chats")
async def purge_old_chats_api(days: int = Query(20, ge=1, le=365)):
    """حذف شاتات العملاء القديمة التي مضى عليها أكثر من عدد الأيام المحدد (افتراضياً 20 يوماً)"""
    res = await asyncio.to_thread(purge_old_chat_messages, days)
    return res


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
