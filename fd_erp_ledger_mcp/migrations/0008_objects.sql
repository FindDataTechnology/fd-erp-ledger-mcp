-- ERP 业务事件内核 · 对象层（change: establish-erp-event-kernel 2.1/2.2/2.3）
--
-- 数量面的三层结构，与账本的「金额面」严格并列、互不冒充：
--   objects          对象身份（sku / 客户 / 供应商 / 仓库 …）
--   object_events    事件到对象的数量增量（只追加；投影的唯一输入）
--   object_balances  物化余额 + 水位（as_of_event_id）
--
-- 纪律：
--   * 余额 MUST NOT 是可直写字段（沿账本「不设可直写余额字段」同纪律）——
--     只有投影函数能写，外部直写被拒。
--   * 物化值必须可全量重算（fact-ledger-kernel R3）：重算是校验手段，不是读路径。
--   * 单位不同的数量 MUST NOT 相加：同 (对象, 维度) 下出现两个单位即视为冲突。
--
-- 全部 DDL 幂等。

CREATE TABLE IF NOT EXISTS objects (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    object_type TEXT NOT NULL CHECK (btrim(object_type) <> ''),
    object_key  TEXT NOT NULL CHECK (btrim(object_key) <> ''),
    attributes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, object_type, object_key)
);

-- 事件 → 对象数量增量。只追加；同一 (事件, 对象, 维度) 只能有一条，
-- 这是投影幂等的来源（重复投影不产生第二次增量）。
CREATE TABLE IF NOT EXISTS object_events (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    event_id       BIGINT NOT NULL REFERENCES business_events (id),
    object_type    TEXT NOT NULL CHECK (btrim(object_type) <> ''),
    object_key     TEXT NOT NULL CHECK (btrim(object_key) <> ''),
    dimension      JSONB NOT NULL DEFAULT '{}'::jsonb,
    quantity_delta NUMERIC(24,9) NOT NULL,
    unit           TEXT NOT NULL CHECK (btrim(unit) <> ''),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, object_type, object_key, dimension)
);

CREATE INDEX IF NOT EXISTS object_events_key_idx
    ON object_events (tenant_id, object_type, object_key);

CREATE TABLE IF NOT EXISTS object_balances (
    tenant_id     TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    object_type   TEXT NOT NULL CHECK (btrim(object_type) <> ''),
    object_key    TEXT NOT NULL CHECK (btrim(object_key) <> ''),
    -- 维度如 {"warehouse":"WH1","lot":"L2301"}；空对象表示不分维度
    dimension     JSONB NOT NULL DEFAULT '{}'::jsonb,
    quantity      NUMERIC(24,9) NOT NULL,
    unit          TEXT NOT NULL CHECK (btrim(unit) <> ''),
    -- 水位：物化值算到哪一条事件为止（R3「快照带水位」）
    as_of_event_id BIGINT NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, object_type, object_key, dimension)
);

-- 物化余额只允许投影函数写（会话开关 erp.object_projection）。
CREATE OR REPLACE FUNCTION erp_deny_balance_direct_write() RETURNS trigger AS $$
BEGIN
    IF coalesce(current_setting('erp.object_projection', true), '') <> 'on' THEN
        RAISE EXCEPTION
            'ERP_OBJECT_CONFLICT object_balances is projection-only (direct % attempted)',
            TG_OP USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'object_balances_projection_only') THEN
        CREATE TRIGGER object_balances_projection_only
            BEFORE INSERT OR UPDATE ON object_balances
            FOR EACH ROW EXECUTE FUNCTION erp_deny_balance_direct_write();
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION erp_deny_object_event_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ERP_APPEND_ONLY object_events is append-only (attempted %)', TG_OP
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'object_events_append_only') THEN
        CREATE TRIGGER object_events_append_only
            BEFORE UPDATE OR DELETE ON object_events
            FOR EACH ROW EXECUTE FUNCTION erp_deny_object_event_mutation();
    END IF;
END
$$;

-- 全量重算：从 object_events 求某 (对象, 维度) 的余额与水位。
-- 这是校验与重建的唯一口径；读路径走 object_balances。
CREATE OR REPLACE FUNCTION erp_recompute_object_balance(
    p_tenant_id TEXT,
    p_object_type TEXT,
    p_object_key TEXT,
    p_dimension JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE (quantity NUMERIC, unit TEXT, as_of_event_id BIGINT) AS $$
    SELECT COALESCE(SUM(quantity_delta), 0),
           -- 同一 (对象, 维度) 下单位必须唯一；出现多单位时由 erp_object_unit_guard 拦截，
           -- 这里取 min 只为让结果类型稳定，不承担裁决职责。
           MIN(unit),
           COALESCE(MAX(event_id), 0)
      FROM object_events
     WHERE tenant_id = p_tenant_id
       AND object_type = p_object_type
       AND object_key = p_object_key
       AND dimension = p_dimension;
$$ LANGUAGE sql STABLE;

-- 单位冲突守卫：同 (租户, 对象, 维度) 只能有一种单位——
-- 把 kg 与 pcs 相加得到的是无意义的数，宁可拒写也不要静默算错。
CREATE OR REPLACE FUNCTION erp_object_unit_guard() RETURNS trigger AS $$
DECLARE
    v_unit TEXT;
BEGIN
    SELECT unit INTO v_unit FROM object_events
     WHERE tenant_id = NEW.tenant_id
       AND object_type = NEW.object_type
       AND object_key = NEW.object_key
       AND dimension = NEW.dimension
     LIMIT 1;
    IF v_unit IS NOT NULL AND v_unit <> NEW.unit THEN
        RAISE EXCEPTION
            'ERP_OBJECT_CONFLICT object %/% dimension % mixes units % and %',
            NEW.object_type, NEW.object_key, NEW.dimension, v_unit, NEW.unit
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'object_events_unit_guard') THEN
        CREATE TRIGGER object_events_unit_guard
            BEFORE INSERT ON object_events
            FOR EACH ROW EXECUTE FUNCTION erp_object_unit_guard();
    END IF;
END
$$;

-- 投影入口：写增量 + 物化余额。幂等——同 (事件, 对象, 维度) 重复投影不产生第二次增量。
CREATE OR REPLACE FUNCTION erp_project_object_event(
    p_event_id BIGINT,
    p_object_type TEXT,
    p_object_key TEXT,
    p_dimension JSONB,
    p_quantity_delta NUMERIC,
    p_unit TEXT
) RETURNS BIGINT AS $$
DECLARE
    v_tenant TEXT;
    v_inserted INT;
    v_qty NUMERIC;
    v_water BIGINT;
BEGIN
    SELECT tenant_id INTO v_tenant FROM business_events WHERE id = p_event_id;
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'ERP_EVENT_UNKNOWN_TYPE no event id %', p_event_id USING ERRCODE = 'P0002';
    END IF;

    INSERT INTO object_events (tenant_id, event_id, object_type, object_key,
                               dimension, quantity_delta, unit)
    VALUES (v_tenant, p_event_id, p_object_type, p_object_key,
            COALESCE(p_dimension, '{}'::jsonb), p_quantity_delta, p_unit)
    ON CONFLICT (event_id, object_type, object_key, dimension) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    IF v_inserted = 0 THEN
        -- 幂等：重复投影返回既有余额，不叠加
        SELECT quantity INTO v_qty FROM object_balances
         WHERE tenant_id = v_tenant AND object_type = p_object_type
           AND object_key = p_object_key AND dimension = COALESCE(p_dimension, '{}'::jsonb);
        RETURN COALESCE(v_qty, 0);
    END IF;

    INSERT INTO objects (tenant_id, object_type, object_key)
    VALUES (v_tenant, p_object_type, p_object_key)
    ON CONFLICT (tenant_id, object_type, object_key) DO NOTHING;

    SELECT quantity, as_of_event_id INTO v_qty, v_water
      FROM erp_recompute_object_balance(v_tenant, p_object_type, p_object_key,
                                        COALESCE(p_dimension, '{}'::jsonb));

    PERFORM set_config('erp.object_projection', 'on', true);
    INSERT INTO object_balances (tenant_id, object_type, object_key, dimension,
                                 quantity, unit, as_of_event_id)
    VALUES (v_tenant, p_object_type, p_object_key, COALESCE(p_dimension, '{}'::jsonb),
            v_qty, p_unit, v_water)
    ON CONFLICT (tenant_id, object_type, object_key, dimension) DO UPDATE
        SET quantity = EXCLUDED.quantity,
            unit = EXCLUDED.unit,
            as_of_event_id = EXCLUDED.as_of_event_id,
            computed_at = now();
    PERFORM set_config('erp.object_projection', 'off', true);

    RETURN v_qty;
END;
$$ LANGUAGE plpgsql;
