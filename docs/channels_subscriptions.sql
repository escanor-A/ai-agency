-- ===============================================================
-- 📱 قنوات السوشيال ميديا واشتراكات التجار (Social Channels Subscriptions)
-- شغّل هذا الكود في Supabase SQL Editor لتمكين التحكم في قنوات التجار
-- (فيسبوك، إنستجرام، تيك توك، واتساب) وتحديد كوتة الواتساب.
-- ===============================================================

ALTER TABLE public.tenants 
ADD COLUMN IF NOT EXISTS allow_facebook BOOLEAN DEFAULT true,
ADD COLUMN IF NOT EXISTS allow_instagram BOOLEAN DEFAULT false,
ADD COLUMN IF NOT EXISTS allow_tiktok BOOLEAN DEFAULT false,
ADD COLUMN IF NOT EXISTS allow_whatsapp BOOLEAN DEFAULT false,
ADD COLUMN IF NOT EXISTS whatsapp_monthly_limit INTEGER DEFAULT 500,
ADD COLUMN IF NOT EXISTS whatsapp_used_conversations INTEGER DEFAULT 0;

-- 💡 تحديث التجار الحاليين لضمان استمرار عمل القنوات المربوطة مسبقاً
UPDATE public.tenants
SET allow_facebook = true
WHERE allow_facebook IS NULL;

-- تفعيل إنستجرام تلقائياً للتجار الذين لديهم حساب إنستجرام مربوط بالفعل
UPDATE public.tenants
SET allow_instagram = true
WHERE allow_instagram IS NULL AND instagram_account_id IS NOT NULL;

-- تفعيل تيك توك تلقائياً للتجار الذين لديهم حساب تيك توك مربوط بالفعل
UPDATE public.tenants
SET allow_tiktok = true
WHERE allow_tiktok IS NULL AND tiktok_account_id IS NOT NULL;

-- ⚠️ ملاحظة بخصوص الواتساب:
-- يظل الواتساب مقفولاً افتراضياً (false) إلا لو التاجر اشترك وفعّله الأدمن
-- لحماية المنصة من تكلفة رسائل Meta Cloud API.

