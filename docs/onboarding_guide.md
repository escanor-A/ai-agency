# 🚀 دليل ربط تاجر جديد — خطوة بخطوة

---

## ⚡ الخطوة 1 — إنشاء حساب التاجر في السيستم

في بوت تليجرام بتاعك `@ORIK_1_bot`، ابعت:

```
/new_tenant اسم المحل 01012345678
```

**مثال:**
```
/new_tenant محل الأناقة 01098765432
```

✅ البوت هيرد بـ **كود ربط** زي `A3F9C2` — ابعته للتاجر.

---

## ⚡ الخطوة 2 — التاجر يربط بوت تليجرامه

التاجر يفتح `@ORIK_1_bot` ويبعت:

```
/start A3F9C2
```

✅ التاجر اتربط — دلوقتي هتوصله إشعارات الأوردرات ويقدر يدير مخزنه.

---

## ⚡ الخطوة 3 — شوف رقم التاجر (tenant_id)

```
/tenants
```

البوت هيعرض كل التجار وأرقامهم وأي سوشيال مربوط.

---

## ⚡ الخطوة 4 — ربط السوشيال ميديا

> **القاعدة:** كل المنصات بيتربطوا بأمر واحد: `/link_page`

---

### 📘 فيسبوك ماسنجر + الكومنتات

**من فين تجيب البيانات:**

| البيانة | من فين |
|---------|--------|
| `Page ID` | facebook.com/yourpage → About → Page ID |
| `Page Token` | developers.facebook.com → App → Messenger → توكن الصفحة |

**الأمر:**
```
/link_page 1 fb=116xxxxxxxxx token=EAABxx...
```

**في Meta Developers — Webhook:**
- URL: `https://yourserver.com/webhook/meta`
- Verify Token: `my_secret_agency_token_2026`
- Subscribe: ✅ `messages` ✅ `feed` (للكومنتات)

---

### 📸 إنستجرام DM + الكومنتات

**شرط:** الأكونت Business أو Creator + مربوط بفيسبوك

**من فين تجيب البيانات:**

| البيانة | من فين |
|---------|--------|
| `Instagram Account ID` | Meta Business Suite → الأكونت → About |
| `Token` | نفس توكن الصفحة على فيسبوك |

**الأمر:**
```
/link_page 1 ig=17xxxxxxxxx
```

> ملحوظة: نفس `token` بتاع فيسبوك بيشتغل للإنستجرام!

**في Meta Developers — Webhook:**
- نفس الـ URL بالضبط
- Subscribe: ✅ `messages` ✅ `comments`

---

### 🟢 واتساب (WhatsApp Cloud API)

**من فين تجيب البيانات:**

| البيانة | من فين |
|---------|--------|
| `Phone Number ID` | developers.facebook.com → WhatsApp → Getting Started → رقم الهاتف |
| `WA Token` | نفس الصفحة → Access Token |

**الأمر:**
```
/link_page 1 wa=10xxxxxxxxx wa_token=EAABxx...
```

**في Meta Developers — Webhook:**
- نفس الـ URL بالضبط
- Subscribe: ✅ `messages`

---

### 🎵 تيك توك

**شرط:** التطبيق محتاج Approval من TikTok (بياخد 3-7 أيام)

**من فين تجيب البيانات:**

| البيانة | من فين |
|---------|--------|
| `TikTok Account ID` | developers.tiktok.com → Dashboard → Account ID |
| `TikTok Token` | OAuth flow بعد الـ Approval |

**الأمر:**
```
/link_page 1 tiktok=7xxxxxxxxx tt_token=TKxx...
```

**في TikTok Developers — Webhook:**
- URL: `https://yourserver.com/webhook/tiktok`
- Events: ✅ `comment.create` ✅ `direct_message.create`

---

## ✅ التأكد إن كل حاجة شغالة

```
/tenants
```

المفروض تشوف كل حاجة مربوطة (مش ❌):

```
1. محل الأناقة
   📘 116xxx | 📸 17xxx | 🟢 10xxx | 🎵 7xxx | ✈️ مربوط
```

---

## 📦 ملخص أوامر ربط تاجر كامل (كلها في أمر واحد!)

```
/link_page 1 fb=116xxx ig=17xxx wa=10xxx tiktok=7xxx token=EAABxx... wa_token=EAABxx... tt_token=TKxx...
```

---

## 🗄️ SQL في Supabase (مرة واحدة بس)

```sql
-- عمود available للمخزون (محسوب تلقائياً)
ALTER TABLE variants
  ADD COLUMN IF NOT EXISTS available INT 
  GENERATED ALWAYS AS (stock - reserved) STORED;

-- أعمدة التوكنات للمنصات
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS facebook_page_token    TEXT,
  ADD COLUMN IF NOT EXISTS whatsapp_access_token  TEXT,
  ADD COLUMN IF NOT EXISTS tiktok_account_id      TEXT,
  ADD COLUMN IF NOT EXISTS tiktok_access_token    TEXT;
```

---

## 🖥️ تشغيل السيرفر

```bash
# بوت تليجرام (شغّله في background)
python telegram_bot.py

# webhook server (محتاج سيرفر عام بـ HTTPS)
uvicorn server:app --host 0.0.0.0 --port 8000
```

