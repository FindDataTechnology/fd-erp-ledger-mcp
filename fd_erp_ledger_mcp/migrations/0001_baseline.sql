-- ERP 账本内核 baseline schema（Postgres）。
-- db-schema-lifecycle 纪律：本文件是唯一权威版本化历史；全部 DDL 幂等（可重放、
-- 可被既有库 adopt）；应用运行时绝不发 DDL，只在 `migrate` 阶段执行。
-- 借贷必平 / append-only / 无出处汇率拒绝 / 幂等唯一键全部由数据库层强制。

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accounts (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    account_type  TEXT NOT NULL CHECK (account_type IN ('asset','liability','equity','revenue','expense')),
    currency      TEXT NOT NULL DEFAULT 'CNY',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code)
);

-- 拟落账事件（proposal）。proposal 绝不改变账本状态：它独立成表，
-- 唯一落账路径是 confirm_proposal。status 只允许 pending -> confirmed/rejected 一次。
CREATE TABLE IF NOT EXISTS proposals (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    stable_id      TEXT NOT NULL CHECK (btrim(stable_id) <> ''),
    kind           TEXT NOT NULL DEFAULT 'journal' CHECK (kind IN ('journal','reversal')),
    payload        JSONB NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','rejected')),
    reversal_of    TEXT,
    source_ref     TEXT,
    -- 柏讯 propose/confirm 契约的 fleet identity turn 车道语义对齐字段
    lane_fleet     TEXT,
    lane_post      TEXT,
    lane_title     TEXT,
    lane_target    TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at     TIMESTAMPTZ,
    decided_by     TEXT,
    decision_note  TEXT,
    UNIQUE (tenant_id, stable_id)
);

CREATE INDEX IF NOT EXISTS proposals_status_idx ON proposals (tenant_id, status, created_at);

-- 双面分录：一行 = 一条借/贷分录行；txn_id 把一次落账的 2+ 行分组。
-- 余额从不存储，永远由本表确定性重算（D3）。
CREATE TABLE IF NOT EXISTS journal_entries (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    txn_id        TEXT NOT NULL,
    line_no       INT  NOT NULL,
    account_code  TEXT NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('debit','credit')),
    amount        NUMERIC(20,6) NOT NULL CHECK (amount > 0),   -- 原币金额
    currency      TEXT NOT NULL,
    amount_base   NUMERIC(20,6) NOT NULL CHECK (amount_base > 0), -- 本位币折算
    fx_rate       NUMERIC(20,10),
    fx_as_of      DATE,
    fx_source     TEXT,
    stable_id     TEXT NOT NULL CHECK (btrim(stable_id) <> ''),
    source_ref    TEXT,
    is_reversal   BOOLEAN NOT NULL DEFAULT false,
    reversal_of   TEXT,
    posted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    provenance    JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- 幂等唯一键：同 stable_id 的同一行不可重复落账
    UNIQUE (tenant_id, stable_id, line_no),
    CONSTRAINT erp_fx_unsourced CHECK (
        (currency = 'CNY' AND fx_rate IS NULL AND fx_as_of IS NULL AND fx_source IS NULL)
        OR
        (fx_rate IS NOT NULL AND fx_rate > 0 AND fx_as_of IS NOT NULL
         AND fx_source IS NOT NULL AND btrim(fx_source) <> '')
    )
);

CREATE INDEX IF NOT EXISTS journal_entries_acct_idx ON journal_entries (tenant_id, account_code);
CREATE INDEX IF NOT EXISTS journal_entries_txn_idx  ON journal_entries (tenant_id, txn_id);
CREATE INDEX IF NOT EXISTS journal_entries_rev_idx  ON journal_entries (tenant_id, reversal_of);

-- 借贷必平：DEFERRABLE 约束触发器，在 COMMIT 时按 txn 分组校验，
-- 应用层无法绕过（双分录可在同一事务内先后插入，提交时才判定）。
CREATE OR REPLACE FUNCTION erp_assert_txn_balanced() RETURNS trigger AS $$
DECLARE
    v_txn     text;
    v_debit   numeric;
    v_credit  numeric;
    v_lines   int;
    v_tenants int;
BEGIN
    v_txn := COALESCE(NEW.txn_id, OLD.txn_id);
    SELECT COALESCE(SUM(amount_base) FILTER (WHERE direction = 'debit'), 0),
           COALESCE(SUM(amount_base) FILTER (WHERE direction = 'credit'), 0),
           COUNT(*),
           COUNT(DISTINCT tenant_id)
      INTO v_debit, v_credit, v_lines, v_tenants
      FROM journal_entries WHERE txn_id = v_txn;
    IF v_tenants > 1 THEN
        RAISE EXCEPTION 'ERP_TENANT txn % mixes tenants', v_txn USING ERRCODE = '23514';
    END IF;
    IF v_lines < 2 THEN
        RAISE EXCEPTION 'ERP_UNBALANCED txn % has fewer than 2 lines', v_txn USING ERRCODE = '23514';
    END IF;
    IF v_debit <> v_credit THEN
        RAISE EXCEPTION 'ERP_UNBALANCED txn % debit % <> credit %', v_txn, v_debit, v_credit
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'journal_entries_balance_deferred' AND n.nspname = current_schema()) THEN
        CREATE CONSTRAINT TRIGGER journal_entries_balance_deferred
            AFTER INSERT OR UPDATE OR DELETE ON journal_entries
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION erp_assert_txn_balanced();
    END IF;
END
$$;

-- append-only：任何 UPDATE/DELETE 已落账分录都被数据库拒绝。
-- 更正只能走冲正（新增反向分录 + reversal_of 关联）。
CREATE OR REPLACE FUNCTION erp_deny_journal_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ERP_APPEND_ONLY journal_entries is append-only (attempted %)', TG_OP
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'journal_entries_append_only' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER journal_entries_append_only
            BEFORE UPDATE OR DELETE ON journal_entries
            FOR EACH ROW EXECUTE FUNCTION erp_deny_journal_mutation();
    END IF;
END
$$;

-- TRUNCATE 同样是删除路径，默认拒绝；仅显式维护会话（erp.allow_maintenance=on）
-- 可做用例隔离/运维清理。这样「删除已落账分录的路径 MUST NOT 存在」对任何
-- 普通连接成立，超级用户也不例外。
CREATE OR REPLACE FUNCTION erp_deny_journal_truncate() RETURNS trigger AS $$
BEGIN
    IF coalesce(current_setting('erp.allow_maintenance', true), '') <> 'on' THEN
        RAISE EXCEPTION 'ERP_APPEND_ONLY journal_entries is append-only (attempted TRUNCATE)'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'journal_entries_no_truncate' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER journal_entries_no_truncate
            BEFORE TRUNCATE ON journal_entries
            FOR EACH STATEMENT EXECUTE FUNCTION erp_deny_journal_truncate();
    END IF;
END
$$;

-- 双保险：应用角色（非超级用户）连 UPDATE/DELETE/TRUNCATE 权限都不给。
REVOKE UPDATE, DELETE, TRUNCATE ON journal_entries FROM PUBLIC;

-- 汇率事实：出处必填（无出处汇率拒绝）。做种走 fx.seed_fx_facts(ecb-exr 观测流)。
CREATE TABLE IF NOT EXISTS fx_facts (
    id             BIGSERIAL PRIMARY KEY,
    base_currency  TEXT NOT NULL DEFAULT 'CNY',
    quote_currency TEXT NOT NULL,
    rate           NUMERIC(20,10) NOT NULL CHECK (rate > 0),
    as_of          DATE NOT NULL,
    source         TEXT NOT NULL CHECK (btrim(source) <> ''),
    provenance     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (base_currency, quote_currency, as_of, source)
);

-- 对账任务（长任务，durable）与逐条结果（仅失败条目可重试）。
CREATE TABLE IF NOT EXISTS recon_reports (
    task_id     TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('queued','running','done','failed','interrupted')),
    processed   INT NOT NULL DEFAULT 0,
    total       INT NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT 'queued',
    result      JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recon_task_items (
    task_id    TEXT NOT NULL,
    item_key   TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('pending','ok','failed')),
    reason     TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, item_key)
);

-- 审计：跨租户拒绝、确认/拒绝决策、冲正等全部留痕。
CREATE TABLE IF NOT EXISTS audit (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'system',
    action      TEXT NOT NULL,
    target      TEXT,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_tenant_idx ON audit (tenant_id, created_at);
