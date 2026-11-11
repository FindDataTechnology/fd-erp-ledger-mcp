-- ERP 业务事件内核 · 过账规则与账本投影（change: establish-erp-event-kernel 3.1-3.5）
--
-- 结构与纪律：
--   posting_rules   版本化过账规则；同 (租户, 事件类型) 只允许一条 active
--   posting_links   事件 → 分录事务 的追溯链（一个事件只过一次账：幂等来源）
--   posting_gaps    缺规则待补清单（不猜科目；事件留在待补里可查）
--
-- 为什么用 posting_links 而不是给 journal_entries 加 event_id：
-- 账本老表一个字节都不动，追溯链放在新表——这样回滚只需停投影与过账，
-- 不需要对账本做任何逆向迁移。追溯能力等价（event → txn → 分录行）。
--
-- 全部 DDL 幂等。

CREATE TABLE IF NOT EXISTS posting_rules (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    event_type  TEXT NOT NULL REFERENCES event_types (event_type),
    version     INT  NOT NULL CHECK (version > 0),
    status      TEXT NOT NULL CHECK (status IN ('draft','active','retired')),
    -- { "lines": [ {"account_code":"1405","direction":"debit","amount_from":"payload.total_amount"}, ... ] }
    rule        JSONB NOT NULL,
    source_ref  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  TEXT NOT NULL DEFAULT 'agent',
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    retired_at  TIMESTAMPTZ,
    UNIQUE (tenant_id, event_type, version),
    -- 生效规则必须带审批痕迹：规则是账本行为的立法，不能无审批生效
    CONSTRAINT posting_rules_approval CHECK (
        status <> 'active' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)
    )
);

-- 同事件类型只能有一条生效规则：两条 active 会让同一事件产出两种科目组合。
CREATE UNIQUE INDEX IF NOT EXISTS posting_rules_one_active
    ON posting_rules (tenant_id, event_type) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS posting_links (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    event_id   BIGINT NOT NULL UNIQUE REFERENCES business_events (id),
    rule_id    BIGINT REFERENCES posting_rules (id),
    txn_id     TEXT NOT NULL,
    lines      INT  NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS posting_links_txn_idx ON posting_links (tenant_id, txn_id);

CREATE TABLE IF NOT EXISTS posting_gaps (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    event_id    BIGINT NOT NULL REFERENCES business_events (id),
    event_type  TEXT NOT NULL,
    reason      TEXT NOT NULL CHECK (btrim(reason) <> ''),
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

-- 同一事件同时只能有一个未解除的缺口（解除后可再登记）
CREATE UNIQUE INDEX IF NOT EXISTS posting_gaps_open
    ON posting_gaps (event_id) WHERE resolved_at IS NULL;
