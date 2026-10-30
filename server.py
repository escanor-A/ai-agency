import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import time
import httpx
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, Response, Query, BackgroundTasks, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from check_stock import supabase
from customer_agent import handle_customer_message, RATE_LIMITER, classify_comment, pop_customer_images
import orders
from orders import is_shipping_enabled
from merchant_settings import load_merchant_settings, save_merchant_settings
from notify import send_complaint_notification, submit_praise_review

from collections import OrderedDict

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_startup_warnings()
    yield


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


async def send_public_comment_reply(comment_id: str, text: str, page_access_token: str):
    """الرد العام تحت الكومنت مباشرة على فيسبوك"""
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
    """الرد على كومنت إنستجرام"""
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
    params = {"fields": "message,story", "access_token": page_access_token}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                msg = (data.get("message") or data.get("story") or "").strip()
                POST_TEXT_CACHE[post_id] = (msg, now)
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
                PARENT_PAGE_CACHE[parent_id] = (is_page, now)
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

            page_token = tenant.get("facebook_page_token", "")
            print(f"📩 رسالة جديدة للتاجر «{tenant['name']}» من العميل {sender_id}: {text}")

            # تشغيل الذكاء الاصطناعي بشكل غير معطل للـ Event Loop (non-blocking)
            meta_channel = "instagram" if str(tenant.get("instagram_account_id") or "") == str(recipient_id) else "messenger"
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
                    public_teaser = f"أهلاً {sender_name} ❤️ تم إرسال كافة التفاصيل والأسعار والمقاسات لحضرتك في رسائل الصفحة على الخاص (شيك على الـ DM) 📩🙏"
                    await send_public_comment_reply(comment_id, public_teaser, page_token)

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
                    dm_teaser = f"أهلاً {sender_name} ❤️ تم إرسال كافة التفاصيل والأسعار والمقاسات لحضرتك في الـ DM، شيك على الرسائل الخاصة 📩🙏"
                    await send_instagram_comment_reply(comment_id, dm_teaser, page_token)
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
    """الرد على كومنت تيك توك عبر TikTok Open API v2"""
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
            data = r.json()
            if data.get("error", {}).get("code") == "ok":
                print(f"✅ تم الرد على كومنت تيك توك {comment_id}")
            else:
                print(f"❌ خطأ في الرد على تيك توك:", data.get("error"))
        except Exception as e:
            print(f"❌ استثناء تيك توك:", type(e).__name__)


async def send_tiktok_dm(user_id: str, text: str, access_token: str):
    """إرسال رسالة مباشرة DM على تيك توك (TikTok Business Messaging)"""
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
            data = r.json()
            if data.get("error", {}).get("code") == "ok":
                print(f"✅ DM تيك توك اتبعت للعميل {user_id}")
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

        # تصنيف التعليق
        category = await asyncio.to_thread(classify_comment, comment_text, "")
        if category == "skip":
            print(f"⏩ تم تخطي كومنت تيك توك المصنف skip: {comment_text}")
            return

        elif category == "complaint":
            await asyncio.to_thread(
                send_complaint_notification,
                tenant["id"],
                customer_name=display_name,
                comment_text=comment_text,
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
            teaser = f"أهلاً {display_name} ❤️ تم إرسال كافة التفاصيل والأسعار والمقاسات في الرسائل الخاصة، شيك على الخاص 📩🙏"
            if tiktok_token and video_id and comment_id:
                await send_tiktok_comment_reply(video_id, comment_id, teaser, tiktok_token)
            return

        elif category in ("location", "hours", "shipping", "offer"):
            full_reply = await asyncio.to_thread(
                handle_customer_message,
                tenant["id"], user_id,
                f"علّقت على فيديو تيك توك وكتبت: {comment_text}",
                channel="tiktok",
                allow_orders=False,
                is_comment=True,
            )
            if tiktok_token and video_id and comment_id and full_reply and full_reply.strip():
                await send_tiktok_comment_reply(video_id, comment_id, full_reply.strip(), tiktok_token)
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
        "stats": stats,
    }


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/dashboard/{tenant_id}", response_class=HTMLResponse)
async def serve_merchant_dashboard(tenant_id: Optional[str] = None):
    """عرض صفحة لوحة التحكم المتوافقة مع المتصفح و Telegram Web App"""
    if os.path.exists(DASHBOARD_TEMPLATE_PATH):
        try:
            with open(DASHBOARD_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        except Exception as e:
            return HTMLResponse(f"<h3>خطأ أثناء قراءة القالب: {e}</h3>", status_code=500)
    return HTMLResponse("<h3>قالب لوحة التحكم غير موجود</h3>", status_code=404)


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

    raise HTTPException(status_code=404, detail="لم يتم العثور على متجر مرتبط برقم التيليجرام هذا")


@app.get("/api/merchants")
async def list_merchants_summary_api():
    """قائمة سريعة بأسماء ومعرّفات المتاجر للاختيار"""
    try:
        res = supabase.table("tenants").select("id, name, bot_enabled").order("name").execute()
        return {"status": "success", "data": res.data or []}
    except Exception as e:
        return {"status": "error", "data": [], "detail": str(e)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
