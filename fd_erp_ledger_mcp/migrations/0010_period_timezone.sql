-- 定型边界必须时区显式（change: establish-erp-event-kernel，缺陷修复）
--
-- 缺陷（2026-11-12 实测抓到）：0007 的 erp_event_is_finalized() 直接对 TIMESTAMPTZ
-- 做 date_trunc，而 date_trunc(text, timestamptz) 的周期边界按**会话 TimeZone** 解释。
-- 后果：同一条日粒度事件，在 +08 的连接上于本地午夜定型，在 UTC 的连接上晚 8 小时
-- 定型——「事实什么时候不可再改」变成取决于谁连的库。对账、审计、冲正窗口全受影响。
--
-- 更糟的是它骗过了测试：原用例用「今天 00:00 UTC」推边界，在本地 23:47 跑恰好过，
-- 在本地 00:42 跑就红——绿灯与否取决于跑测试的时刻。这类假绿比红更危险。
--
-- 修法：周期边界按 event_types.period_timezone 显式解释（默认 UTC），
-- 与会话 TimeZone 解耦；并加一条「换会话时区答案不变」的回归用例。

ALTER TABLE event_types
    ADD COLUMN IF NOT EXISTS period_timezone TEXT NOT NULL DEFAULT 'UTC';

-- 函数体改为：先把 timestamptz 落到目标时区的墙上时间，按粒度截断，再加一个周期，
-- 最后转回 timestamptz。这正是 date_trunc 在跨时区场景下的标准写法。
CREATE OR REPLACE FUNCTION erp_event_is_finalized(
    p_occurred_at TIMESTAMPTZ,
    p_granularity TEXT,
    p_revision_window INTERVAL,
    p_as_of TIMESTAMPTZ DEFAULT now(),
    p_timezone TEXT DEFAULT 'UTC'
) RETURNS BOOLEAN AS $$
DECLARE
    v_period_end TIMESTAMPTZ;
BEGIN
    IF p_granularity = 'immediate' THEN
        RETURN TRUE;
    ELSIF p_granularity = 'never' THEN
        RETURN FALSE;
    ELSIF p_granularity NOT IN ('day', 'month', 'year') THEN
        RAISE EXCEPTION 'ERP_EVENT_UNKNOWN_TYPE 未知定型粒度 %', p_granularity
            USING ERRCODE = '22023';
    END IF;

    v_period_end := (
        date_trunc(p_granularity, p_occurred_at AT TIME ZONE p_timezone)
        + CASE p_granularity
              WHEN 'day'   THEN INTERVAL '1 day'
              WHEN 'month' THEN INTERVAL '1 month'
              ELSE              INTERVAL '1 year'
          END
    ) AT TIME ZONE p_timezone;

    RETURN p_as_of >= (v_period_end + COALESCE(p_revision_window, INTERVAL '0'));
END;
$$ LANGUAGE plpgsql STABLE;

-- 换掉旧签名（4 参 IMMUTABLE 版）：保留 4 参重载会让人以为它仍然可用。
DROP FUNCTION IF EXISTS erp_event_is_finalized(TIMESTAMPTZ, TEXT, INTERVAL, TIMESTAMPTZ);

-- 触发器与修订入口都要按事件类型声明的时区判定，不能各自取默认。
CREATE OR REPLACE FUNCTION erp_deny_event_mutation() RETURNS trigger AS $$
DECLARE
    v_finalized BOOLEAN;
    v_granularity TEXT;
    v_window INTERVAL;
    v_tz TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ERP_APPEND_ONLY business_events is append-only (attempted DELETE of id %)',
            OLD.id USING ERRCODE = '55000';
    END IF;

    IF NEW.tenant_id   IS DISTINCT FROM OLD.tenant_id
       OR NEW.event_type  IS DISTINCT FROM OLD.event_type
       OR NEW.stable_id   IS DISTINCT FROM OLD.stable_id
       OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
       OR NEW.lane        IS DISTINCT FROM OLD.lane
       OR NEW.source_ref  IS DISTINCT FROM OLD.source_ref
       OR NEW.reversal_of IS DISTINCT FROM OLD.reversal_of THEN
        RAISE EXCEPTION 'ERP_EVENT_IMMUTABLE business_events id % value fields are immutable',
            OLD.id USING ERRCODE = '55000';
    END IF;

    IF NEW.payload IS DISTINCT FROM OLD.payload THEN
        IF coalesce(current_setting('erp.event_revision', true), '') <> 'on' THEN
            RAISE EXCEPTION
                'ERP_EVENT_IMMUTABLE business_events id % payload immutable; use erp_revise_event()',
                OLD.id USING ERRCODE = '55000';
        END IF;
        SELECT finality_granularity, revision_window, period_timezone
          INTO v_granularity, v_window, v_tz
          FROM event_types WHERE event_type = OLD.event_type;
        v_finalized := erp_event_is_finalized(OLD.occurred_at, v_granularity, v_window,
                                              now(), v_tz);
        IF v_finalized THEN
            RAISE EXCEPTION 'ERP_EVENT_IMMUTABLE business_events id % is finalized (occurred_at %)',
                OLD.id, OLD.occurred_at USING ERRCODE = '55000';
        END IF;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'pending'  AND NEW.status IN ('accepted','rejected'))
            OR (OLD.status = 'accepted' AND NEW.status = 'reversed')
        ) THEN
            RAISE EXCEPTION 'ERP_EVENT_IMMUTABLE illegal status transition % -> % for id %',
                OLD.status, NEW.status, OLD.id USING ERRCODE = '55000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION erp_revise_event(
    p_tenant_id TEXT,
    p_stable_id TEXT,
    p_lane      TEXT,
    p_payload   JSONB,
    p_revised_by TEXT,
    p_reason    TEXT DEFAULT '',
    p_as_of     TIMESTAMPTZ DEFAULT now()
) RETURNS BIGINT AS $$
DECLARE
    v_row business_events%ROWTYPE;
    v_granularity TEXT;
    v_window INTERVAL;
    v_tz TEXT;
BEGIN
    SELECT * INTO v_row FROM business_events
     WHERE tenant_id = p_tenant_id AND stable_id = p_stable_id AND lane = p_lane
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ERP_EVENT_UNKNOWN_TYPE no event %/%/%', p_tenant_id, p_lane, p_stable_id
            USING ERRCODE = 'P0002';
    END IF;

    SELECT finality_granularity, revision_window, period_timezone
      INTO v_granularity, v_window, v_tz
      FROM event_types WHERE event_type = v_row.event_type;

    IF erp_event_is_finalized(v_row.occurred_at, v_granularity, v_window, p_as_of, v_tz) THEN
        RAISE EXCEPTION 'ERP_EVENT_IMMUTABLE event % is finalized; submit a reversal instead', v_row.id
            USING ERRCODE = '55000';
    END IF;

    INSERT INTO event_revisions (event_id, old_payload, new_payload, revised_by, reason)
    VALUES (v_row.id, v_row.payload, p_payload, p_revised_by, p_reason);

    PERFORM set_config('erp.event_revision', 'on', true);
    UPDATE business_events SET payload = p_payload WHERE id = v_row.id;
    PERFORM set_config('erp.event_revision', 'off', true);

    RETURN v_row.id;
END;
$$ LANGUAGE plpgsql;
