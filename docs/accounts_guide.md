# 🧾 دليل إنشاء الحسابات وربطها بالسيستم

---

## أولاً — إيه الحسابات اللي محتاجها؟

```
🔵 Meta Business Account    ← أساس كل حاجة (فيسبوك، إنستجرام، واتساب)
🟣 Meta Developer App       ← الجسر بين السيستم والمنصات
🟢 WhatsApp Business        ← منتج داخل الـ Developer App
🎵 TikTok Developer App     ← منفصل تماماً
```

> **حساب واحد على Meta يكفي للـ 3 منصات (FB + IG + WA)**

---

---

# 🔵 PART 1 — Meta (فيسبوك + إنستجرام + واتساب)

## الخطوة 1 — إنشاء Meta Business Account

**مرة واحدة بس — مش للتاجر، ده حسابك أنت كأجنسي!**

1. روح: **business.facebook.com**
2. اضغط **Create Account**
3. ادخل:
   - اسم الأجنسي (مثلاً: ORIK Agency)
   - اسمك
   - إيميلك
4. ✅ تم — جاهز

---

## الخطوة 2 — إنشاء Meta Developer App

**مرة واحدة بس كمان!**

1. روح: **developers.facebook.com**
2. اضغط **My Apps** → **Create App**
3. اختار نوع التطبيق: **Business**
4. ادخل اسم للتطبيق (مثلاً: ORIK Bot)
5. اختار البزنس أكونت اللي أنشأته في الخطوة 1
6. ✅ تم — عندك App ID وApp Secret

---

## الخطوة 3 — ربط الـ Webhook بالتطبيق

**في نفس التطبيق:**

1. من القائمة الجانبية: **Webhooks**
2. اضغط **Subscribe to this object** → اختار **Page**
3. ادخل:
   - **Callback URL:** `https://yourserver.com/webhook/meta`
   - **Verify Token:** `my_secret_agency_token_2026`
4. اضغط **Verify and Save**
5. اشترك في الـ fields دي:
   - ✅ `messages`
   - ✅ `messaging_postbacks`
   - ✅ `feed` (للكومنتات)

---

## الخطوة 4 — إضافة منتج Messenger

**عشان ترد على رسائل فيسبوك:**

1. في التطبيق → **Add Product** → **Messenger** → Setup
2. في قسم **Access Tokens** → اختار صفحة التاجر
3. اضغط **Generate Token**
4. 📋 **انسخ الـ Token ده** — ده `page_token`

**احفظ كمان:**
- اضغط على اسم الصفحة → شوف **Page ID** في الرابط أو في Settings

---

## الخطوة 5 — إضافة منتج Instagram

**عشان ترد على DMs والكومنتات:**

1. في التطبيق → **Add Product** → **Instagram** → Setup
2. اربط حساب إنستجرام التاجر بالصفحة:
   - Meta Business Suite → Settings → Instagram Accounts → Add
3. من التطبيق → Instagram → Get Instagram Account ID
4. 📋 **انسخ الـ Instagram Account ID**

> ⚠️ نفس الـ `page_token` بتاع فيسبوك بيشتغل هنا!

---

## الخطوة 6 — إضافة منتج WhatsApp

**عشان ترد على واتساب:**

1. في التطبيق → **Add Product** → **WhatsApp** → Setup
2. اختار الـ Business Account
3. هتلاقي:
   - **Phone Number ID** → 📋 انسخه
   - **Temporary Access Token** → 📋 انسخه
4. في قسم Webhooks → اشترك في: ✅ `messages`

> ⚠️ الـ Temporary Token بينتهي بعد 24 ساعة!
> للإنتاج محتاج **Permanent Token** من Business Manager

---

## الخطوة 7 — ربط التاجر في السيستم

الأمر هيكون كده (غيّر الأرقام بالأرقام الحقيقية):

```
/link_page 1 fb=116xxxxxxxxx ig=17xxxxxxxxx wa=10xxxxxxxxx token=EAABxx... wa_token=EAABxx...
```

---

---

# 🎵 PART 2 — تيك توك (أصعب شوية)

## الخطوة 1 — إنشاء TikTok Developer Account

1. روح: **developers.tiktok.com**
2. اضغط **Sign In** → سجّل بحساب تيك توك عادي أو بإيميل
3. اضغط **Manage Apps** → **Create App**
4. ادخل:
   - اسم التطبيق
   - Category: **Business**
   - Platform: **Web**
5. ✅ تم — عندك **Client Key** و**Client Secret**

---

## الخطوة 2 — طلب الصلاحيات (Approval)

**ده اللي بياخد وقت!**

1. في التطبيق → **Products** → اضغط **Add**
2. اطلب:
   - ✅ **Comment** — عشان تقرأ وترد على الكومنتات
   - ✅ **Direct Message** — عشان ترد على الـ DMs
3. اضغط **Submit for Review**
4. ⏳ انتظر 3-7 أيام

---

## الخطوة 3 — OAuth للتاجر (بعد الـ Approval)

بعد الموافقة، محتاج تعمل OAuth flow للتاجر:

1. وجّه التاجر لرابط:
   ```
   https://www.tiktok.com/auth/authorize/
     ?client_key=YOUR_CLIENT_KEY
     &response_type=code
     &scope=comment.list,comment.create,message.dm
     &redirect_uri=https://yourserver.com/tiktok/callback
   ```
2. التاجر يوافق
3. بتاخد **Code** وتحوّله لـ **Access Token**

---

## الخطوة 4 — ربط التاجر في السيستم

```
/link_page 1 tiktok=7xxxxxxxxx tt_token=TKAccessTokenHena...
```

---

---

# 📋 جدول المقارنة السريع

| المنصة | نوع الحساب | الوقت | التوكن بيتجدد؟ |
|--------|-----------|-------|----------------|
| **فيسبوك** | Meta Business + App | 10 دقائق | ❌ دايم |
| **إنستجرام** | نفس App الفيسبوك | 5 دقائق | ❌ دايم |
| **واتساب** | نفس App الفيسبوك | 10 دقائق | ⚠️ مؤقت (محتاج permanent) |
| **تيك توك** | TikTok Developer | 3-7 أيام (approval) | ✅ كل فترة |

---

# 🔑 ملخص — إيه اللي هتاخده من كل منصة

| البيانة | من فين | بتحطها فين |
|---------|--------|-----------|
| `fb=` Facebook Page ID | facebook.com/yourpage → About | `/link_page` |
| `token=` Page Access Token | developers.facebook.com → Messenger → Tokens | `/link_page` |
| `ig=` Instagram Account ID | Meta Business Suite → Instagram | `/link_page` |
| `wa=` WhatsApp Phone Number ID | developers.facebook.com → WhatsApp → Setup | `/link_page` |
| `wa_token=` WA Access Token | نفس الصفحة | `/link_page` |
| `tiktok=` TikTok Account ID | developers.tiktok.com | `/link_page` |
| `tt_token=` TikTok Access Token | OAuth flow | `/link_page` |

