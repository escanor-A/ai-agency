-- ========================================================================
-- دالة تنظيف رسائل المحادثات القديمة تلقائياً بعد مرور 20 يوماً
-- للحفاظ على مساحة قاعدة البيانات في Supabase وحذف سجلات الدردشة القديمة
-- ========================================================================

CREATE OR REPLACE FUNCTION purge_old_chat_messages(p_days INT DEFAULT 20)
RETURNS TABLE (
    deleted_messages_count INT,
    deleted_conversations_count INT
) 
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_cutoff TIMESTAMPTZ := NOW() - (p_days || ' days')::INTERVAL;
    v_del_msgs INT := 0;
    v_del_convs INT := 0;
BEGIN
    -- 1. حذف الرسائل التي مر عليها أكثر من 20 يوماً
    WITH deleted AS (
        DELETE FROM public.messages
        WHERE created_at < v_cutoff
        RETURNING id
    )
    SELECT COUNT(*) INTO v_del_msgs FROM deleted;

    -- 2. حذف المحادثات القديمة التي ليس بها أي رسائل وآخر تحديث لها قبل 20 يوماً
    WITH deleted_convs AS (
        DELETE FROM public.conversations c
        WHERE c.updated_at < v_cutoff
          AND NOT EXISTS (
              SELECT 1 FROM public.messages m WHERE m.conversation_id = c.id
          )
        RETURNING id
    )
    SELECT COUNT(*) INTO v_del_convs FROM deleted_convs;

    RETURN QUERY SELECT v_del_msgs, v_del_convs;
END;
$$;

-- لتشغيل التنظيف يدوياً في أي وقت من SQL Editor في Supabase:
-- SELECT * FROM purge_old_chat_messages(20);

-- (اختياري) لتفعيل الجدولة التلقائية يومياً في Supabase باستخدام امتداد pg_cron (لو مفعّل):
-- SELECT cron.schedule('daily_chat_purge', '0 3 * * *', 'SELECT purge_old_chat_messages(20);');
