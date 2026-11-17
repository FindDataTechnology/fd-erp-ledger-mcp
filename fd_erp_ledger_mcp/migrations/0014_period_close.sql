-- 业务日期 + 期间关账（change: add-period-close）
--
-- 问题：journal_entries 只有 posted_at（落账时刻），没有业务日期。于是「10 月的单据在 11 月
-- 才确认」这件事表达不出来，期间归属实际上等于「谁什么时候点的确认」。后果：报表事后会变
-- （同一期利润表今天跑和下月跑可以不同，两次都「正确」），关账也无处落地（拦不住任何东西）。
--
-- 修法一：业务日期独立成列。期间 = to_char(booked_on,'YYYY-MM')，booked_on 是 DATE，
-- 不含时区，因此期间归属**与会话 TimeZone 彻底解耦**（0010 在事件定型边界上栽过一次：
-- 对 TIMESTAMPTZ 做 date_trunc 会按会话时区解释周期边界，同一条事实在两台机器上于不同时刻定型）。
--
-- 回填：历史行按**显式**时区取日期，绝不用 session TimeZone。存量部署的会话时区是 +08，
-- 故取 'Asia/Shanghai'；这个选择一次性且写死在迁移里，不让运行时猜（猜错就是历史期间整体偏移）。
--
-- 修法二：accounting_periods 追加式记录 close/reopen。当前状态 = 该期间最新一条记录，
-- 没有可变的 status 列（可变状态列会被 UPDATE 悄悄改回，而「关账被改回」是最难发现的账务事故）。
-- 落账闸由 BEFORE INSERT 触发器强制：应用层少一处判断就漏一处，数据库层不会漏。

ALTER TABLE proposals       ADD COLUMN IF NOT EXISTS booked_on DATE;
ALTER TABLE journal_entries ADD COLUMN IF NOT EXISTS booked_on DATE;

-- 回填（幂等：只填 NULL 行）。必须先临时关掉 append-only 触发器——它按设计拒绝一切
-- UPDATE 已落账分录，而回填是**迁移的维护动作**，不是业务更正（业务更正仍只走冲正）。
-- 迁移一次性、单事务、已登记版本，回填后立刻恢复触发器；这不是可复用的绕过口子。
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'journal_entries_append_only' AND n.nspname = current_schema()) THEN
        ALTER TABLE journal_entries DISABLE TRIGGER journal_entries_append_only;
    END IF;
END
$$;

UPDATE journal_entries
   SET booked_on = (posted_at AT TIME ZONE 'Asia/Shanghai')::date
 WHERE booked_on IS NULL;

-- 借贷必平是 DEFERRABLE 约束触发器：上面那条 UPDATE 排了待触发的检查事件，此时
-- ALTER TABLE 会被拒（ObjectInUse: pending trigger events）。先把它们就地触发完
-- （回填不改金额，必然通过），再恢复成延迟——否则后续落账会从「提交时判定」退化成
-- 「每条语句判定」，把一条正常的双分录写入拆死。
SET CONSTRAINTS ALL IMMEDIATE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'journal_entries_append_only' AND n.nspname = current_schema()) THEN
        ALTER TABLE journal_entries ENABLE TRIGGER journal_entries_append_only;
    END IF;
END
$$;

SET CONSTRAINTS ALL DEFERRED;

UPDATE proposals
   SET booked_on = (created_at AT TIME ZONE 'Asia/Shanghai')::date
 WHERE booked_on IS NULL;

-- 回填之后才能收紧：分录的业务日期不允许缺失（缺了就等于「不知道这笔属于哪期」）。
-- proposals 给一个 DB 默认值当安全网：应用总会显式传（按 Settings.book_timezone 算），
-- 但裸 SQL 插入也不该因为漏传而落一个不可归属的行。
ALTER TABLE proposals ALTER COLUMN booked_on
    SET DEFAULT ((now() AT TIME ZONE 'Asia/Shanghai')::date);
ALTER TABLE proposals       ALTER COLUMN booked_on SET NOT NULL;
ALTER TABLE journal_entries ALTER COLUMN booked_on SET NOT NULL;

CREATE INDEX IF NOT EXISTS journal_entries_period_idx
    ON journal_entries (tenant_id, booked_on);
CREATE INDEX IF NOT EXISTS proposals_period_idx
    ON proposals (tenant_id, booked_on);

-- 期间台账：只有追加，无状态列。snapshot 在关账那一刻写入，是「当时报出去的数字」本身；
-- 事后复算与快照对比，不一致就说明有人动过东西（或关账后又落了账）。
CREATE TABLE IF NOT EXISTS accounting_periods (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    period      TEXT NOT NULL CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    action      TEXT NOT NULL CHECK (action IN ('close','reopen')),
    actor       TEXT NOT NULL CHECK (btrim(actor) <> ''),
    note        TEXT,
    snapshot    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS accounting_periods_idx
    ON accounting_periods (tenant_id, period, id);

-- 追加式：UPDATE/DELETE 一律拒绝（关账记录本身也是账务事实，只能追加新记录改状态）
CREATE OR REPLACE FUNCTION erp_deny_period_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ERP_APPEND_ONLY accounting_periods is append-only (attempted %)', TG_OP
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'accounting_periods_append_only' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER accounting_periods_append_only
            BEFORE UPDATE OR DELETE ON accounting_periods
            FOR EACH ROW EXECUTE FUNCTION erp_deny_period_mutation();
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION erp_deny_period_truncate() RETURNS trigger AS $$
BEGIN
    IF coalesce(current_setting('erp.allow_maintenance', true), '') <> 'on' THEN
        RAISE EXCEPTION 'ERP_APPEND_ONLY accounting_periods is append-only (attempted TRUNCATE)'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'accounting_periods_no_truncate' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER accounting_periods_no_truncate
            BEFORE TRUNCATE ON accounting_periods
            FOR EACH STATEMENT EXECUTE FUNCTION erp_deny_period_truncate();
    END IF;
END
$$;

-- 落账闸：业务日期落在已关期间的分录进不来。
-- 前缀 ERP_PERIOD_CLOSED 由 errors.map_psycog_error 翻译成封闭码 PERIOD_CLOSED；
-- 漏了那条映射，这里就会塌成 UPSTREAM_UNAVAILABLE（调用方以为库抖了，其实是期间关了）。
CREATE OR REPLACE FUNCTION erp_assert_period_open() RETURNS trigger AS $$
DECLARE
    v_period TEXT;
    v_action TEXT;
BEGIN
    v_period := to_char(NEW.booked_on, 'YYYY-MM');
    SELECT action INTO v_action
      FROM accounting_periods
     WHERE tenant_id = NEW.tenant_id AND period = v_period
     ORDER BY id DESC
     LIMIT 1;
    IF v_action = 'close' THEN
        RAISE EXCEPTION
            'ERP_PERIOD_CLOSED 期间 % 已关账（booked_on=%），拒绝落账', v_period, NEW.booked_on
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = 'journal_entries_period_open' AND n.nspname = current_schema()) THEN
        CREATE TRIGGER journal_entries_period_open
            BEFORE INSERT ON journal_entries
            FOR EACH ROW EXECUTE FUNCTION erp_assert_period_open();
    END IF;
END
$$;
