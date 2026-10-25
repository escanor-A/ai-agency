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
    """إرسال الرد للعميل على ماسنجر أو إنستجرام"""
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
            print(f"❌ استثناء في إرسال {platform}:", e)


async def process_meta_event(data: dict):
    """معالجة الرسائل الواردة من فيسبوك وإنستجرام وواتساب وتحديد التاجر"""
    entry_list = data.get("entry", [])
    for entry in entry_list:
        # أ) رسائل فيسبوك ماسنجر & إنستجرام
        messaging_list = entry.get("messaging", [])
        for event in messaging_list:
            sender_id = event.get("sender", {}).get("id")
            recipient_id = event.get("recipient", {}).get("id") # ده الـ page_id أو instagram_id
            message = event.get("message", {})
            text = message.get("text")

            # تجاهل الرسائل اللي البوت باعتها بنفسه (echo)
            if not text or message.get("is_echo"):
                continue

            # البحث عن التاجر صاحب هذه الصفحة أو هذا الحساب
            rows = (
                supabase.table("tenants")
                .select("id, name, facebook_page_id, instagram_account_id")
                .or_(f"facebook_page_id.eq.{recipient_id},instagram_account_id.eq.{recipient_id}")
                .execute()
                .data
            )
            if not rows:
                print(f"⚠️ صفحة غير مسجلة في النظام: {recipient_id}")
                continue

            tenant = rows[0]
            print(f"📩 رسالة جديدة للتاجر «{tenant['name']}» من العميل {sender_id}: {text}")

            # تشغيل الذكاء الاصطناعي الخاص بالتاجر ده
            reply = handle_customer_message(tenant["id"], sender_id, text)

            # إرسال الرد للعميل (محتاج التوكن المخزن لصفحة التاجر)
            # await send_meta_reply(sender_id, reply, page_token)
            print(f"🤖 رد البوت للعميل: {reply}")

        # ب) رسائل واتساب (WhatsApp Business Cloud API)
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            messages = value.get("messages", [])
            for msg in messages:
                if msg.get("type") == "text":
                    wa_from = msg.get("from")
                    wa_text = msg.get("text", {}).get("body")
                    
                    # البحث عن التاجر برقم الواتساب
                    rows = (
                        supabase.table("tenants")
                        .select("id, name")
                        .eq("whatsapp_phone_number_id", phone_number_id)
                        .execute()
                        .data
                    )
                    if rows:
                        tenant = rows[0]
                        reply = handle_customer_message(tenant["id"], wa_from, wa_text)
                        print(f"🟢 واتساب «{tenant['name']}» -> رد للعميل {wa_from}: {reply}")


@app.post("/webhook/meta")
async def receive_meta_webhook(request: Request, background_tasks: BackgroundTasks):
    """استقبال رسائل فيسبوك، إنستجرام، واتساب في الخلفية"""
    payload = await request.json()
    background_tasks.add_task(process_meta_event, payload)
    return {"status": "received"}


# ==========================================
# 2. TikTok Webhook
# ==========================================

@app.post("/webhook/tiktok")
async def receive_tiktok_webhook(request: Request, background_tasks: BackgroundTasks):
    """استقبال رسائل تيك توك Webhook"""
    payload = await request.json()
    print("🎵 رسالة واردة من تيك توك:", payload)
    
    event = payload.get("event")
    # منطق تيك توك لاستخراج المرسل والرسالة ومعرف حساب التاجر
    # TikTok Business Messaging API
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
