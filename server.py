import os
import sys
import httpx
from fastapi import FastAPI, Request, Response, Query, BackgroundTasks
from dotenv import load_dotenv

from check_stock import supabase
from customer_agent import handle_customer_message

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

app = FastAPI(title="AI Agency Omnichannel Webhook Server")

# توكن التحقق السري الخاص بك لربط Meta Webhooks (تقدر تخليه أي كلمة سر تختارها)
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "my_secret_agency_token_2026")


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
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        print("✅ تم التحقق من Meta Webhook بنجاح!")
        return Response(content=challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)


async def send_meta_reply(recipient_id: str, text: str, page_access_token: str, platform: str = "messenger"):
    """إرسال رد خاص (DM) للعميل على ماسنجر أو إنستجرام"""
    url = f"https://graph.facebook.com/v21.0/me/messages?access_token={page_access_token}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                print(f"❌ خطأ في إرسال الرد لـ {platform}:", r.text)
        except Exception as e:
            print(f"❌ استثناء في إرسال {platform}:", type(e).__name__)


async def send_public_comment_reply(comment_id: str, text: str, page_access_token: str):
    """الرد العام تحت الكومنت مباشرة على فيسبوك"""
    url = f"https://graph.facebook.com/v21.0/{comment_id}/comments"
    params = {"access_token": page_access_token}
    payload = {"message": text}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, params=params, timeout=10)
            if r.status_code != 200:
                print(f"❌ خطأ في الرد على الكومنت:", r.text[:200])
            else:
                print(f"✅ تم الرد العام على الكومنت {comment_id}")
        except Exception as e:
            print(f"❌ استثناء في الرد على الكومنت:", type(e).__name__)


async def send_instagram_comment_reply(comment_id: str, text: str, page_access_token: str):
    """الرد على كومنت إنستجرام"""
    url = f"https://graph.facebook.com/v21.0/{comment_id}/replies"
    params = {"access_token": page_access_token}
    payload = {"message": text}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, params=params, timeout=10)
            if r.status_code != 200:
                print(f"❌ خطأ في الرد على كومنت إنستجرام:", r.text[:200])
            else:
                print(f"✅ تم الرد على كومنت إنستجرام {comment_id}")
        except Exception as e:
            print(f"❌ استثناء إنستجرام:", type(e).__name__)


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


async def process_meta_event(data: dict):
    """معالجة الرسائل الواردة من فيسبوك وإنستجرام وواتساب وتحديد التاجر"""
    entry_list = data.get("entry", [])
    for entry in entry_list:
        # أ) رسائل فيسبوك ماسنجر & إنستجرام
        messaging_list = entry.get("messaging", [])
        for event in messaging_list:
            sender_id = event.get("sender", {}).get("id")
            recipient_id = event.get("recipient", {}).get("id")  # ده الـ page_id أو instagram_id
            message = event.get("message", {})
            text = message.get("text")

            # تجاهل الرسائل اللي البوت باعتها بنفسه (echo)
            if not text or message.get("is_echo"):
                continue

            # البحث عن التاجر صاحب هذه الصفحة أو هذا الحساب
            rows = (
                supabase.table("tenants")
                .select("id, name, facebook_page_id, instagram_account_id, facebook_page_token")
                .or_(f"facebook_page_id.eq.{recipient_id},instagram_account_id.eq.{recipient_id}")
                .execute()
                .data
            )
            if not rows:
                print(f"⚠️ صفحة غير مسجلة في النظام: {recipient_id}")
                continue

            tenant = rows[0]
            page_token = tenant.get("facebook_page_token", "")
            print(f"📩 رسالة جديدة للتاجر «{tenant['name']}» من العميل {sender_id}: {text}")

            # تشغيل الذكاء الاصطناعي الخاص بالتاجر ده
            reply = handle_customer_message(tenant["id"], sender_id, text)
            print(f"🤖 رد البوت: {reply}")

            # إرسال الرد للعميل
            if page_token:
                await send_meta_reply(sender_id, reply, page_token)
            else:
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
                sender_id = value.get("from", {}).get("id")
                sender_name = value.get("from", {}).get("name", "يا فندم")
                comment_text = value.get("message", "")
                page_id = entry.get("id")

                if sender_id == page_id or not comment_text:
                    continue

                rows = (
                    supabase.table("tenants")
                    .select("id, name, facebook_page_token")
                    .eq("facebook_page_id", page_id)
                    .execute()
                    .data
                )
                if rows:
                    tenant = rows[0]
                    page_token = tenant.get("facebook_page_token", "")
                    print(f"💬 تعليق على بوست «{tenant['name']}» من {sender_name}: {comment_text}")

                    # الـ AI بيولّد الرد الكامل
                    full_reply = handle_customer_message(
                        tenant["id"], sender_id,
                        f"أنا علقت على بوست وكتبت: {comment_text}"
                    )

                    if page_token:
                        # 1️⃣ رد عام قصير تحت الكومنت مباشرة — يشجع يدخل الـ DM
                        public_teaser = f"أهلاً {sender_name} ❤️ راجعنا على الرسائل الخاصة هنا في الصفحة وهنرد عليك فوراً 🙏"
                        await send_public_comment_reply(comment_id, public_teaser, page_token)

                        # 2️⃣ رد خاص (DM ماسنجر) بالإجابة الكاملة من الـ AI
                        await send_meta_reply(sender_id, full_reply, page_token)
                    else:
                        print(f"⚠️ التاجر «{tenant['name']}» مش عنده page_token — استخدم /link_page")

            # 1b. تعليقات إنستجرام
            elif field == "comments" and value.get("verb") == "add":
                comment_id = value.get("id")
                sender_id = value.get("from", {}).get("id")
                sender_name = value.get("from", {}).get("name", "يا فندم")
                comment_text = value.get("text", "")
                ig_media_id = value.get("media", {}).get("id", "")
                page_id = entry.get("id")

                if not comment_text or not comment_id:
                    continue

                rows = (
                    supabase.table("tenants")
                    .select("id, name, facebook_page_token")
                    .eq("instagram_account_id", page_id)
                    .execute()
                    .data
                )
                if rows:
                    tenant = rows[0]
                    page_token = tenant.get("facebook_page_token", "")
                    print(f"📸 كومنت إنستجرام على «{tenant['name']}» من {sender_name}: {comment_text}")

                    full_reply = handle_customer_message(
                        tenant["id"], sender_id,
                        f"علّقت على بوست وكتبت: {comment_text}"
                    )

                    if page_token:
                        # رد مباشر تحت الكومنت على إنستجرام
                        await send_instagram_comment_reply(comment_id, full_reply, page_token)
                    else:
                        print(f"⚠️ التاجر «{tenant['name']}» مش عنده page_token")



            # 2. رسائل واتساب
            elif field == "messages":
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                messages = value.get("messages", [])
                for msg in messages:
                    if msg.get("type") == "text":
                        wa_from = msg.get("from")
                        wa_text = msg.get("text", {}).get("body")

                        rows = (
                            supabase.table("tenants")
                            .select("id, name, whatsapp_phone_number_id, whatsapp_access_token")
                            .eq("whatsapp_phone_number_id", phone_number_id)
                            .execute()
                            .data
                        )
                        if rows:
                            tenant = rows[0]
                            wa_token = tenant.get("whatsapp_access_token", "")
                            reply = handle_customer_message(tenant["id"], wa_from, wa_text)
                            print(f"🟢 واتساب «{tenant['name']}» -> رد للعميل {wa_from}: {reply}")
                            if wa_token and phone_number_id:
                                await send_whatsapp_reply(wa_from, reply, phone_number_id, wa_token)
                            else:
                                print(f"⚠️ التاجر «{tenant['name']}» مش عنده wa_token — استخدم /link_page wa_token=...")



@app.post("/webhook/meta")
async def receive_meta_webhook(request: Request, background_tasks: BackgroundTasks):
    """استقبال رسائل فيسبوك، إنستجرام، واتساب في الخلفية"""
    payload = await request.json()
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
        account_id  = data.get("owner_id", "")   # معرف صاحب الفيديو (التاجر)

        if not comment_text:
            return

        # البحث عن التاجر بـ tiktok_account_id
        rows = (
            supabase.table("tenants")
            .select("id, name, tiktok_access_token")
            .eq("tiktok_account_id", account_id)
            .execute()
            .data
        )
        if not rows:
            print(f"⚠️ حساب تيك توك غير مسجّل: {account_id}")
            return

        tenant = rows[0]
        tiktok_token = tenant.get("tiktok_access_token", "")
        print(f"🎵 كومنت على «{tenant['name']}» من {display_name}: {comment_text}")

        # الـ AI بيولّد الرد
        full_reply = handle_customer_message(
            tenant["id"], user_id,
            f"علّقت على فيديو تيك توك وكتبت: {comment_text}"
        )

        if tiktok_token and video_id and comment_id:
            await send_tiktok_comment_reply(video_id, comment_id, full_reply, tiktok_token)
        else:
            print(f"⚠️ التاجر «{tenant['name']}» مش عنده tiktok_token — استخدم /link_page")

    # ───── رسائل DM تيك توك ─────
    elif event_type == "direct_message.create":
        sender_id    = data.get("sender_id", "")
        display_name = data.get("sender_display_name", "يا فندم")
        message_text = data.get("message", {}).get("text", "")
        account_id   = data.get("receiver_id", "")   # حساب التاجر المستقبِل

        if not message_text:
            return

        rows = (
            supabase.table("tenants")
            .select("id, name, tiktok_access_token")
            .eq("tiktok_account_id", account_id)
            .execute()
            .data
        )
        if not rows:
            print(f"⚠️ حساب تيك توك غير مسجّل: {account_id}")
            return

        tenant = rows[0]
        tiktok_token = tenant.get("tiktok_access_token", "")
        print(f"🎵 DM تيك توك لـ «{tenant['name']}» من {display_name}: {message_text}")

        reply = handle_customer_message(tenant["id"], sender_id, message_text)

        if tiktok_token:
            await send_tiktok_dm(sender_id, reply, tiktok_token)
        else:
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
    """استقبال أحداث تيك توك (كومنتات + DMs) في الخلفية"""
    payload = await request.json()
    background_tasks.add_task(process_tiktok_event, payload)
    return {"status": "received"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
