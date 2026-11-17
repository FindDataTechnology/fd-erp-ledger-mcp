-- 计费规则引擎 + 信用额度（erp-vertical-templates 任务 1.x / 4.2）。
-- db-schema-lifecycle 纪律同 0001：唯一权威版本化历史、全 DDL 幂等可重放；
-- 应用运行时绝不发 DDL。
-- 关键不变量：
--   * fee_details append-only（BEFORE UPDATE/DELETE/TRUNCATE 触发器拒绝 + REVOKE
--     双保险，照抄 journal_entries 做法）；红冲 = 新增反向行（reversal_of 关联），
--     原行原样保留，无任何改/删路径。
--   * 计费幂等：UNIQUE(tenant_id, stable_id) 兜底，同一 stable_id 重推返回首次结果。
--   * 计费快照：每行费用锁定 rule_id + rule_version + 基础数据快照（snapshot jsonb），
--     可离线重算一致。
--   * 信用占用是备忘性质（MUST NOT 写成复式分录）；额度口径 = 账本衍生（设计已裁定）。

-- 计费规则（版本化）。同 rule_key 多版本递增；版本一经提交不可改写，
-- 状态机 draft -> pending_approval -> active / retired 由审批动作推进。
CREATE TABLE IF NOT EXISTS billing_rules (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    rule_key        TEXT NOT NULL CHECK (btrim(rule_key) <> ''),
    version         INT  NOT NULL CHECK (version >= 1),
    -- config：收费项(fee_item)/事件类型(event_kind)/计价模式(mode)/费率(必须带
    -- rate_source 出处)/阶梯(tiers)/封顶(cap_amount)/最低消费(minimum_amount)/
    -- 生效期(effective_from/effective_to)/显式优先级(priority)
    config          JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','pending_approval','active','retired')),
    source_ref      TEXT,
    provenance      JSONB NOT NULL DEFAULT '{}'::jsonb,
    submitted_by    TEXT,
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, rule_key, version)
);

CREATE INDEX IF NOT EXISTS billing_rules_status_idx ON billing_rules (tenant_id, status, created_at);

-- 计费明细（append-only 事实行）。snapshot 锁定「事件 + 费率（含出处）+ 基础数据」，
-- provenance 承载四层溯源链：费用项 -> 业务单据 -> 作业事件 -> 计费规则版本。
-- status：normal = 原始计费行；reversed = 红冲反向行（reversal_of 指向原行 id）。
-- 原行永不变更（append-only），「已被红冲」由存在反向行派生，不落任何可写状态。
CREATE TABLE IF NOT EXISTS fee_details (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    rule_id          BIGINT NOT NULL REFERENCES billing_rules(id),
    rule_version     INT  NOT NULL,
    stable_id        TEXT NOT NULL CHECK (btrim(stable_id) <> ''),
    event_stable_id  TEXT NOT NULL CHECK (btrim(event_stable_id) <> ''),
    fee_item         TEXT NOT NULL CHECK (btrim(fee_item) <> ''),
    snapshot         JSONB NOT NULL,
    amount           NUMERIC(20,6) NOT NULL CHECK (amount >= 0),
    currency         TEXT NOT NULL,
    provenance       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'normal' CHECK (status IN ('normal','reversed')),
    reversal_of      BIGINT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, stable_id)
);

CREATE INDEX IF NOT EXISTS fee_details_event_idx   ON fee_details (tenant_id, event_stable_id);
CREATE INDEX IF NOT EXISTS fee_details_rule_idx    ON fee_details (tenant_id, rule_id);
CREATE INDEX IF NOT EXISTS fee_details_reverse_idx ON fee_details (tenant_id, reversal_of);

-- append-only：任何 UPDATE/DELETE 已生成费用明细都被数据库拒绝。
-- 更正只走红冲（新增 status='reversed' 反向行 + reversal_of 关联），原行保持原样。
CREATE OR REPLACE FUNCTION erp_deny_fee_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ERP_APPEND_ONLY fee_details is append-only (attempted %)', TG_OP
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'fee_details_append_only' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER fee_details_append_only
            BEFORE UPDATE OR DELETE ON fee_details
            FOR EACH ROW EXECUTE FUNCTION erp_deny_fee_mutation();
    END IF;
END
$$;

-- TRUNCATE 同样是删除路径，默认拒绝；仅显式维护会话（erp.allow_maintenance=on）
-- 可做用例隔离/运维清理（与 journal_entries 同一开关、同一纪律）。
CREATE OR REPLACE FUNCTION erp_deny_fee_truncate() RETURNS trigger AS $$
BEGIN
    IF coalesce(current_setting('erp.allow_maintenance', true), '') <> 'on' THEN
        RAISE EXCEPTION 'ERP_APPEND_ONLY fee_details is append-only (attempted TRUNCATE)'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'fee_details_no_truncate' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER fee_details_no_truncate
            BEFORE TRUNCATE ON fee_details
            FOR EACH STATEMENT EXECUTE FUNCTION erp_deny_fee_truncate();
    END IF;
END
$$;

-- 双保险：应用角色连 UPDATE/DELETE/TRUNCATE 权限都不给。
REVOKE UPDATE, DELETE, TRUNCATE ON fee_details FROM PUBLIC;

-- 信用额度登记（备忘性质，非复式分录）。额度口径 = 账本衍生（limit - 1300 应收
-- 余额 - active 占用之和），本表只存额度上限事实（带出处）。
CREATE TABLE IF NOT EXISTS credit_registry (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    customer_id   TEXT NOT NULL CHECK (btrim(customer_id) <> ''),
    limit_amount  NUMERIC(20,6) NOT NULL CHECK (limit_amount >= 0),
    currency      TEXT NOT NULL,
    source_ref    TEXT,
    provenance    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, customer_id)
);

-- 信用占用（备忘）：status reserved/released；同 stable_id 幂等（重推返回首次结果）。
-- 占用 MUST NOT 写成复式分录（设计已裁定）；release 是本表状态推进，非账本事实。
CREATE TABLE IF NOT EXISTS credit_reservations (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    customer_id   TEXT NOT NULL CHECK (btrim(customer_id) <> ''),
    stable_id     TEXT NOT NULL CHECK (btrim(stable_id) <> ''),
    amount        NUMERIC(20,6) NOT NULL CHECK (amount > 0),
    currency      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','released')),
    source_ref    TEXT,
    provenance    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at   TIMESTAMPTZ,
    UNIQUE (tenant_id, stable_id)
);

CREATE INDEX IF NOT EXISTS credit_reservations_customer_idx
    ON credit_reservations (tenant_id, customer_id, status);
