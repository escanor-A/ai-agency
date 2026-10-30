-- إضافة عمود shipping_enabled لجدول tenants لتمكين تشغيل/إيقاف الشحن لكل تاجر
ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS shipping_enabled BOOLEAN DEFAULT true;

