-- إضافة عمود min_order_value لجدول العروض promotions لدعم عروض الفاوتشر وكروت الهدايا (اشتري بقيمة X واحصل على Y)
ALTER TABLE public.promotions ADD COLUMN IF NOT EXISTS min_order_value NUMERIC DEFAULT 0;
