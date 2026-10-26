-- ===============================================================
-- 📊 جدول الاشتراكات السحابي (Database Subscriptions Table)
-- شغل هذا الكود في Supabase SQL Editor لنقل الاشتراكات من JSON إلى الداتابيز
-- ===============================================================

CREATE TABLE IF NOT EXISTS public.subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    ends_at TIMESTAMPTZ NOT NULL,
    months INTEGER DEFAULT 1,
    warning_sent BOOLEAN DEFAULT false,
    expired_alert_sent BOOLEAN DEFAULT false,
    updated_at TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT subscriptions_tenant_id_unique UNIQUE (tenant_id)
);

-- فهرس لتسريع استعلامات الفحص والاشتراكات المنتهية
CREATE INDEX IF NOT EXISTS idx_subscriptions_ends_at ON public.subscriptions(ends_at);
CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant_id ON public.subscriptions(tenant_id);

-- صلاحيات القراءة والتعديل لـ Service Role
ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service Role Full Access" 
ON public.subscriptions 
FOR ALL 
TO service_role 
USING (true) 
WITH CHECK (true);
