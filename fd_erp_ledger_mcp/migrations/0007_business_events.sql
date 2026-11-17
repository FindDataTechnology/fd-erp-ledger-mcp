-- ERP 业务事件内核 · 事件层（change: establish-erp-event-kernel 1.1/1.2）
--
-- 本迁移只新增事件层，不动任何既有表：
--   * event_types      事件类型注册表（封闭集；写入前必须先登记）
--   * business_events  只追加事实
-- 纪律出处：openspec/specs/fact-ledger-kernel（R1 事实只追加 / R2 更正只冲正 /
-- R8 读取自证完整），本文件把 R1 的两种情况都落成数据库约束：
--   定型前可修订但必须留痕（erp_revise_event）；定型后任何改写都被拒。
-- 定型判定按事件自身周期粒度推导（event_types.finality_granularity +
-- revision_window），MUST NOT 用全局秒数——年度事实在周期走完后仍会修订，
-- 把「周期走完」等同于「不再修订」会让修订被判成非法改写。
--
-- 全部 DDL 幂等（db-schema-lifecycle）：应用运行时不发 DDL，只在 migrate 阶段执行。

CREATE TABLE IF NOT EXISTS event_types (
    event_type          TEXT PRIMARY KEY CHECK (btrim(event_type) <> ''),
    description         TEXT NOT NULL DEFAULT '',
    -- 载荷必填字段清单（JSON Schema 子集，供写入校验与文档派生）
    payload_schema      JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- day | month | year | immediate | never
    -- immediate：写入即定型（不可修订）；never：永不自动定型，只能由人工冻结
    finality_granularity TEXT NOT NULL DEFAULT 'day'
        CHECK (finality_granularity IN ('day','month','year','immediate','never')),
    -- 修订窗：在周期走完之后仍允许修订的时长（仅 granularity 非 immediate/never 时有意义）
    revision_window     INTERVAL NOT NULL DEFAULT INTERVAL '0',
    -- 该类型事件是否必须命中过账规则：true 而缺规则 → 落待补清单（不猜科目）
    requires_posting    BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS business_events (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    event_type  TEXT NOT NULL REFERENCES event_types (event_type),
    stable_id   TEXT NOT NULL CHECK (btrim(stable_id) <> ''),
    -- 事实发生时点，与写入时点分离（补录、回填都要能表达真实发生时点）
    occurred_at TIMESTAMPTZ NOT NULL,
    payload     JSONB NOT NULL,
    -- 两条车道：ingest=外部系统原始事实（幂等、可直接成为事实）
    --           draft =agent 产出（MUST 过确认门才成为事实，见 fact-ledger-kernel R5）
    lane        TEXT NOT NULL CHECK (lane IN ('ingest','draft')),
    status      TEXT NOT NULL
        CHECK (status IN ('pending','accepted','rejected','reversed')),
    source_ref  TEXT,
    reversal_of TEXT,
    -- 'unknown' 是一等值时序：payload 中显式未知必须与空串/0 可区分，
    -- 故这里不提供任何把未知填成空串的默认值。
    provenance  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    decided_by  TEXT,
    -- 两条车道各自一个幂等键空间：同 stable_id 在 ingest 与 draft 互不顶掉
    UNIQUE (tenant_id, lane, stable_id),
    -- 拟稿车道事件必须能被确认门前置引用；ingest 车道直接是事实
    CONSTRAINT business_events_draft_pending CHECK (
        lane <> 'draft' OR status <> 'accepted' OR decided_at IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS business_events_type_time_idx
    ON business_events (tenant_id, event_type, occurred_at);
CREATE INDEX IF NOT EXISTS business_events_status_idx
    ON business_events (tenant_id, status, created_at);
CREATE INDEX IF NOT EXISTS business_events_reversal_idx
    ON business_events (tenant_id, reversal_of);

-- 修订留痕表：定型前的修订必须保留被替换的旧值、修订来源与修订时点。
-- 只追加，不允许更新/删除。
CREATE TABLE IF NOT EXISTS event_revisions (
    id          BIGSERIAL PRIMARY KEY,
    event_id    BIGINT NOT NULL REFERENCES business_events (id),
    old_payload JSONB NOT NULL,
    new_payload JSONB NOT NULL,
    revised_by  TEXT NOT NULL CHECK (btrim(revised_by) <> ''),
    reason      TEXT NOT NULL DEFAULT '',
    revised_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS event_revisions_event_idx
    ON event_revisions (event_id, revised_at);

-- 定型判定：按事件自身周期粒度推导，as_of 可显式传入（测试与回放需要确定性）。
CREATE OR REPLACE FUNCTION erp_event_is_finalized(
    p_occurred_at TIMESTAMPTZ,
    p_granularity TEXT,
    p_revision_window INTERVAL,
    p_as_of TIMESTAMPTZ DEFAULT now()
) RETURNS BOOLEAN AS $$
DECLARE
    v_period_end TIMESTAMPTZ;
BEGIN
    IF p_granularity = 'immediate' THEN
        RETURN TRUE;
    ELSIF p_granularity = 'never' THEN
        RETURN FALSE;
    ELSIF p_granularity = 'day' THEN
        v_period_end := date_trunc('day', p_occurred_at) + INTERVAL '1 day';
    ELSIF p_granularity = 'month' THEN
        v_period_end := date_trunc('month', p_occurred_at) + INTERVAL '1 month';
    ELSIF p_granularity = 'year' THEN
        v_period_end := date_trunc('year', p_occurred_at) + INTERVAL '1 year';
    ELSE
        RAISE EXCEPTION 'ERP_EVENT_UNKNOWN_TYPE 未知定型粒度 %', p_granularity
            USING ERRCODE = '22023';
    END IF;
    RETURN p_as_of >= (v_period_end + COALESCE(p_revision_window, INTERVAL '0'));
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- 事实不可改写（R1）。允许的唯一变更路径是 status 的合法迁移
-- （pending -> accepted/rejected，accepted -> reversed），以及经 erp_revise_event()
-- 在定型前对 payload 的修订（该函数会置会话开关并写 event_revisions）。
CREATE OR REPLACE FUNCTION erp_deny_event_mutation() RETURNS trigger AS $$
DECLARE
    v_finalized BOOLEAN;
    v_granularity TEXT;
    v_window INTERVAL;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ERP_APPEND_ONLY business_events is append-only (attempted DELETE of id %)',
            OLD.id USING ERRCODE = '55000';
    END IF;

    -- 值字段一律不可改（无论是否定型）
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
        SELECT finality_granularity, revision_window
          INTO v_granularity, v_window
          FROM event_types WHERE event_type = OLD.event_type;
        v_finalized := erp_event_is_finalized(OLD.occurred_at, v_granularity, v_window);
        IF v_finalized THEN
            RAISE EXCEPTION 'ERP_EVENT_IMMUTABLE business_events id % is finalized (occurred_at %)',
                OLD.id, OLD.occurred_at USING ERRCODE = '55000';
        END IF;
    END IF;

    -- 状态迁移封闭集（R6：生命周期状态封闭且迁移可判非法）
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

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'business_events_no_mutation' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER business_events_no_mutation
            BEFORE UPDATE OR DELETE ON business_events
            FOR EACH ROW EXECUTE FUNCTION erp_deny_event_mutation();
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION erp_deny_event_truncate() RETURNS trigger AS $$
BEGIN
    IF coalesce(current_setting('erp.allow_maintenance', true), '') <> 'on' THEN
        RAISE EXCEPTION 'ERP_APPEND_ONLY business_events is append-only (attempted TRUNCATE)'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'business_events_no_truncate' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER business_events_no_truncate
            BEFORE TRUNCATE ON business_events
            FOR EACH STATEMENT EXECUTE FUNCTION erp_deny_event_truncate();
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION erp_deny_event_revision_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ERP_APPEND_ONLY event_revisions is append-only (attempted %)', TG_OP
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'event_revisions_append_only' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER event_revisions_append_only
            BEFORE UPDATE OR DELETE ON event_revisions
            FOR EACH ROW EXECUTE FUNCTION erp_deny_event_revision_mutation();
    END IF;
END
$$;

-- 定型前修订的唯一合法入口：留旧值、记来源、记时点。
-- p_as_of 仅测试/回放用；生产省略即用 now()。
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
BEGIN
    SELECT * INTO v_row FROM business_events
     WHERE tenant_id = p_tenant_id AND stable_id = p_stable_id AND lane = p_lane
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ERP_EVENT_UNKNOWN_TYPE no event %/%/%', p_tenant_id, p_lane, p_stable_id
            USING ERRCODE = 'P0002';
    END IF;

    SELECT finality_granularity, revision_window INTO v_granularity, v_window
      FROM event_types WHERE event_type = v_row.event_type;

    IF erp_event_is_finalized(v_row.occurred_at, v_granularity, v_window, p_as_of) THEN
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

-- 事件类型封闭集做种（幂等）。新类型必须显式登记：
-- 未登记即写入会被外键拒绝，而不是静默接受一个未定义语义的事实。
-- payload_schema.required 是"可采信"的最小门槛：缺字段或字段为空串即拒写，
-- 确实未知必须显式写 '__unknown__'（未知与没有可区分）。
INSERT INTO event_types (event_type, description, payload_schema, finality_granularity, revision_window, requires_posting)
VALUES
  ('goods_received',  '到货入库（数量面：sku/单位/批次/仓库）',
   '{"required":["sku","quantity","unit","warehouse"],"properties":["sku","quantity","unit","warehouse","lot","serial","po_ref"]}'::jsonb,
   'day', INTERVAL '0', true),
  ('goods_issued',    '出库发运（数量面）',
   '{"required":["sku","quantity","unit"],"properties":["sku","quantity","unit","warehouse","lot"]}'::jsonb,
   'day', INTERVAL '0', true),
  ('goods_adjusted',  '库存调整/盘点差异（数量面）',
   '{"required":["sku","quantity","unit","reason"],"properties":["sku","quantity","unit","warehouse","lot","reason"]}'::jsonb,
   'day', INTERVAL '0', true),
  ('order_placed',    '订单成立（金额面可估值）',
   '{"required":["order_id","total_amount","currency"],"properties":["order_id","total_amount","currency","customer_key"]}'::jsonb,
   'day', INTERVAL '0', true),
  ('invoice_requested','发票申请（只记事件，票据本体走客户渠道）',
   '{"required":["invoice_ref"],"properties":["invoice_ref","order_id","amount"]}'::jsonb,
   'day', INTERVAL '0', false),
  ('payment_received','收款到账（结算流水）',
   '{"required":["amount","currency","channel"],"properties":["amount","currency","channel","order_id","paid_at"]}'::jsonb,
   'day', INTERVAL '0', true)
ON CONFLICT (event_type) DO NOTHING;
