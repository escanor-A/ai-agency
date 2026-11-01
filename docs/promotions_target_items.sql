-- ===============================================================
-- 🏷️ تحديد أصناف العروض والتركيبات (Target Items & Combos)
-- شغّل هذا الكود في Supabase SQL Editor لإضافة عمود target_items
-- لتمكين تحديد أصناف مخصصة (مثل: تيشيرت، هودي) أو أطقم مركبة (تيشيرت + بنطلون).
-- ===============================================================

ALTER TABLE public.promotions 
ADD COLUMN IF NOT EXISTS target_items TEXT DEFAULT NULL;
