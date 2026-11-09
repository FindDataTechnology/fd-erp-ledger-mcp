-- 计费补强（change: optimize-erp-solution 任务 5.1 / 5.5）。
--
-- 纪律同 0001/0004：唯一权威版本化历史；全 DDL 幂等（ADD COLUMN IF NOT EXISTS /
-- 约束存在性探测），可被已有 0004 的库增量执行；运行时绝不发 DDL。
--
-- 本迁移只增列与增表，不删列、不改既有数据（回退应用层即可保留数据）。
--
-- 1) fee_details 折算三要素（design D7）：
--    费用明细 MUST 能独立审计，所以原币金额、本位币金额、汇率/时点/出处一并落明细，
--    而不是「明细只存原币、落账时折算」（那样只有账本能解释金额）。
--    * 存量行（0005 之前写入）不补造口径：original_amount / base_amount / fx_* 留
--      NULL，金额口径仍在 amount 列（= 计价币种金额）。补造折算值等于凭空发明汇率，
--      与 0004「不给孤儿补一个未来时点」同一纪律。
--    * 新写入行由 billing.py 强制成对写入；数据库用 erp_fee_fx_provenance 兜底
--      「汇率三要素要么齐、要么全空」，与 journal_entries.erp_fx_unsourced 同形。
--
-- 2) billing_fee_items 收费项登记（design D9，二选一里选「独立登记表」）：
--    理由：登记描述的是「收费项目录是否在用」，与规则版本状态机（同 rule_key 多版本
--    draft/pending_approval/active/retired）是两件事。把登记标记挂在 billing_rules 上
--    会让「登记」随规则版本演进漂移（退役一个版本是否等于收费项停用？），差集计算也
--    要先去重 rule_key；独立表让「在用集合」有唯一、稳定的来源。
--    event_kind 与 fee_item 同登记：事件计费要能回答「本事件类型本应命哪些在用收费项」，
--    没有这个对应关系就只能用事件流反推（D9 已否）。

ALTER TABLE fee_details ADD COLUMN IF NOT EXISTS original_amount NUMERIC(20,6);
ALTER TABLE fee_details ADD COLUMN IF NOT EXISTS base_amount     NUMERIC(20,6);
ALTER TABLE fee_details ADD COLUMN IF NOT EXISTS fx_rate         NUMERIC(20,10);
ALTER TABLE fee_details ADD COLUMN IF NOT EXISTS fx_as_of        DATE;
ALTER TABLE fee_details ADD COLUMN IF NOT EXISTS fx_source       TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'erp_fee_fx_provenance') THEN
        ALTER TABLE fee_details ADD CONSTRAINT erp_fee_fx_provenance CHECK (
            (fx_rate IS NULL AND fx_as_of IS NULL AND fx_source IS NULL)
            OR
            (fx_rate IS NOT NULL AND fx_rate > 0 AND fx_as_of IS NOT NULL
             AND fx_source IS NOT NULL AND btrim(fx_source) <> '')
        );
    END IF;
END
$$;

-- 金额成对：要么都没写（存量行），要么都写且非负。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'erp_fee_amount_pair') THEN
        ALTER TABLE fee_details ADD CONSTRAINT erp_fee_amount_pair CHECK (
            (original_amount IS NULL AND base_amount IS NULL)
            OR
            (original_amount IS NOT NULL AND original_amount >= 0
             AND base_amount IS NOT NULL AND base_amount >= 0)
        );
    END IF;
END
$$;

COMMENT ON COLUMN fee_details.original_amount IS
    '原币金额（计价币种，= amount 的显式列）；0005 之前的存量行为 NULL';
COMMENT ON COLUMN fee_details.base_amount IS
    '本位币金额（= original_amount × fx_rate）；本位币费用与折算后外币费用均落值';
COMMENT ON COLUMN fee_details.fx_rate IS
    '折算汇率（取自 fx_facts 登记事实）；本位币费用为 NULL';
COMMENT ON COLUMN fee_details.fx_as_of IS
    '汇率时点（汇率事实的 as_of）；本位币费用为 NULL';
COMMENT ON COLUMN fee_details.fx_source IS
    '汇率出处（fx_facts.source，如 ecb-exr）；无出处不入账，本位币费用为 NULL';

-- 收费项登记（在用集合的唯一来源）。status：in_use = 在用（参与覆盖差集判定）；
-- retired = 已停用（退出差集，但保留登记痕，便于回答「这个收费项曾经在用」）。
CREATE TABLE IF NOT EXISTS billing_fee_items (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    fee_item      TEXT NOT NULL CHECK (btrim(fee_item) <> ''),
    -- 该收费项由哪种业务事件触发（事件计费按 event_kind 找「本应命中的在用收费项」）
    event_kind    TEXT NOT NULL CHECK (btrim(event_kind) <> ''),
    status        TEXT NOT NULL DEFAULT 'in_use' CHECK (status IN ('in_use','retired')),
    source_ref    TEXT,
    registered_by TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at    TIMESTAMPTZ,
    UNIQUE (tenant_id, fee_item, event_kind)
);

CREATE INDEX IF NOT EXISTS billing_fee_items_lookup_idx
    ON billing_fee_items (tenant_id, status, event_kind);

COMMENT ON TABLE billing_fee_items IS
    '收费项登记（design D9）：审批上线与事件计费按「登记在用集合 − active 规则集合」'
    '差集阻断覆盖缺失；登记走运维命令 register-fee-item，不做 MCP 工具（工具面口径固定）。';
