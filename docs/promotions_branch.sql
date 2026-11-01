-- ===============================================================
-- 🏬 إضافة عمود الفرع (Branch) لجدول العروض الترويجية promotions
-- ===============================================================

ALTER TABLE public.promotions 
ADD COLUMN IF NOT EXISTS branch TEXT DEFAULT 'كل الفروع';
