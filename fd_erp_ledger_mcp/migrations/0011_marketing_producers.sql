-- 事件类型注册表扩成多板块生产者 · 营销接入（change: fuse-marketing-onto-event-kernel 1.1-1.5）
--
-- 本迁移只扩**类型注册表**，不动 business_events 的只追加/定型/冲正纪律，也不动
-- journal_entries / posting / objects 任何既有逻辑：
--   * event_types 增 producer / domain / evidence_ref 三列（幂等 ADD COLUMN）
--   * 登记三个营销类型：内容发布 / 客服会话关闭 / 渠道订单付款
--   * 注册侧三道闸：名字前缀与 producer 自洽、四类无载体事实黑名单、营销类型必须指出载体
--
-- 为什么 producer 落在类型注册表而不是每条事件自己声明：
-- 板块归属是「这个类型由哪个板块生产」的**注册时**属性，不是写入时的参数。
-- 若允许调用方在 append_event 里声明 producer，等于让调用方自己给自己开板块：
-- 营销侧写一条 erp 类型就能绕开 producer='erp' 的巡检口径，ERP 侧也能把账务事实
-- 标成营销事实。故板块归属只由 event_type → event_types.producer 查得，
-- 调用方传入一律忽略（见 fd_erp_ledger_mcp/events.py 的 append_event 注释）。
--
-- 存量 6 个 ERP 类型由 ADD COLUMN 的 DEFAULT 'erp' 自动落位，不做数据回填：
-- 回填会去改历史行的语义，而它们本来就都是 ERP 生产的。
--
-- 全部 DDL 幂等（db-schema-lifecycle）：可重复执行，不重跑任何破坏性语句。

ALTER TABLE event_types
    ADD COLUMN IF NOT EXISTS producer TEXT NOT NULL DEFAULT 'erp';
ALTER TABLE event_types
    ADD COLUMN IF NOT EXISTS domain TEXT;
-- 载体出处：文件路径 + 符号，或连接器动作名（如 chatwoot:conversations）。
-- 可空只对 ERP 老类型成立；营销类型必填由下面的触发器强制。
ALTER TABLE event_types
    ADD COLUMN IF NOT EXISTS evidence_ref TEXT;

-- 板块是封闭集合：producer 写错一个字母会让类型静默落到「谁也不管」的板块，
-- 前缀触发器只管 marketing./非 marketing. 两种情况，认不出拼错的第三个值。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'event_types_producer_known') THEN
        ALTER TABLE event_types
            ADD CONSTRAINT event_types_producer_known
            CHECK (producer IN ('erp', 'marketing'));
    END IF;
END
$$;

-- D6 的读口径：按板块过滤/停用要能走索引。event_types 是小表，索引不会显著提速，
-- 但「按 producer 停用」是声明过的运维动作，留索引让 EXPLAIN 可读。
CREATE INDEX IF NOT EXISTS event_types_producer_idx ON event_types (producer);

-- 注册侧唯一闸口。三条判据都返回稳定码前缀（errors.map_psycog_error 按字符串匹配）：
--   ERP_EVENT_FABRICATED_FACT     无载体的事实（黑名单）/ 营销类型缺载体出处
--   ERP_EVENT_PRODUCER_MISMATCH   类型名前缀与 producer 不自洽
CREATE OR REPLACE FUNCTION erp_assert_type_producer_consistency() RETURNS trigger AS $$
BEGIN
    -- ① 四类「仓内没有载体」的营销事实：登记即拒，与 producer 无关。
    --    spec MUST NOT 点名 campaign_spend / lead_created / campaign_launched / conversion，
    --    并覆盖 *_spend、lead_*、*_conversion 等变体写法——换个名字不等于有了载体。
    --    点分命名下变体形如 `<域>.spend` / `lead_<对象>` / `<域>.conversion`，故两种形态都收。
    --    判据是**词段**而不是后缀：名字按 `.` / `_` 切段，任一段落在这四个词干上即拒。
    --    只锚后缀会漏掉 `marketing.ad_spend_monthly` 这类把词干夹在中间的名字
    --    （2026-11-12 实测漏过一条才改成按段），而裸子串匹配又会误伤 `conversation`
    --    这类真词，所以两侧必须是分隔符或边界。真要做 ERP 侧 `fx.conversion` 这类
    --    同形名时改名或在此显式解禁——这道摩擦是 D5 有意的。
    --    注册时拒，是因为这四类最容易被「融合」这个名义带进来：它们是营销最想要的
    --    数据，也最容易看起来「填个数字就能用」。
    IF NEW.event_type ~*
       '(^|[._])(campaign|campaigns|spend|spends|lead|leads|conversion|conversions)([._]|$)' THEN
        RAISE EXCEPTION
            'ERP_EVENT_FABRICATED_FACT 类型 % 属于无载体的营销事实：仓内无该事实的任何载体：先补载体再登记类型',
            NEW.event_type USING ERRCODE = '23514';
    END IF;

    -- ② 点分前缀必须与 producer 自洽（D2）：单表里类型名是唯一的人工识别线索，
    --    前缀不一致时人在 query_events 结果里分不清哪条是哪个板块写的。
    IF NEW.producer = 'marketing' AND NEW.event_type NOT LIKE 'marketing.%' THEN
        RAISE EXCEPTION
            'ERP_EVENT_PRODUCER_MISMATCH 类型 % 登记为 producer=marketing，但名字不以 marketing. 开头',
            NEW.event_type USING ERRCODE = '23514';
    END IF;
    IF NEW.producer = 'erp' AND NEW.event_type LIKE 'marketing.%' THEN
        RAISE EXCEPTION
            'ERP_EVENT_PRODUCER_MISMATCH 类型 % 以 marketing. 开头，却登记为 producer=erp',
            NEW.event_type USING ERRCODE = '23514';
    END IF;

    -- ③ 营销类型必须指出载体出处（1.5）：指不到载体的类型此刻没有生产者，只会有编造者。
    --    因此这里复用 FABRICATED_FACT 而不是另开一个码——失败模式与黑名单相同：
    --    「这个类型背后没有任何事实源」。ERP 老类型不受影响（evidence_ref 可空）。
    IF NEW.producer = 'marketing'
       AND (NEW.evidence_ref IS NULL OR btrim(NEW.evidence_ref) = '') THEN
        RAISE EXCEPTION
            'ERP_EVENT_FABRICATED_FACT 营销类型 % 缺 evidence_ref（载体出处）：仓内无该事实的任何载体：先补载体再登记类型',
            NEW.event_type USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'event_types_producer_consistency') THEN
        CREATE TRIGGER event_types_producer_consistency
            BEFORE INSERT OR UPDATE ON event_types
            FOR EACH ROW EXECUTE FUNCTION erp_assert_type_producer_consistency();
    END IF;
END
$$;

-- 登记三个营销类型（幂等）。全部 requires_posting=false：营销事实进事件层后
-- **可以**被 ERP 过账规则消费，但消费是可选的一侧，不是自动合并（不并账）。
-- finality_granularity='day' + revision_window=0 + period_timezone='UTC'：
-- 与 ERP 同口径，定型时刻不随会话时区漂移（0010 的缺陷修法同样适用于新类型）。
-- evidence_ref 写真实载体出处；载荷 required 只写适配器真能填出的字段——
-- 填不出的字段写进 required，等于逼适配器编值。
INSERT INTO event_types (event_type, description, payload_schema, finality_granularity,
                         revision_window, requires_posting, period_timezone,
                         producer, domain, evidence_ref)
VALUES
  ('marketing.content.published',
   '内容发布（营销：fd-wire 中已发布的 artifact 版本行）',
   '{"required":["slug","title","published_at","artifact_ref"],"properties":["slug","title","published_at","artifact_ref"]}'::jsonb,
   'day', INTERVAL '0', false, 'UTC', 'marketing', 'marketing',
   'fd-wire/api/fd_wire_api/artifacts/models.py#artifact_versions'),
  ('marketing.desk.conversation_closed',
   '客服会话关闭（营销：外部 Chatwoot 会话回流，只读）',
   '{"required":["conversation_ref","closed_at","channel"],"properties":["conversation_ref","closed_at","channel"]}'::jsonb,
   'day', INTERVAL '0', false, 'UTC', 'marketing', 'marketing',
   'chatwoot:conversations'),
  ('marketing.channel.order_paid',
   '渠道订单付款（营销：电商连接器只读面回流；付款事实，不是下单事实）',
   '{"required":["order_ref","amount","currency","channel"],"properties":["order_ref","amount","currency","channel"]}'::jsonb,
   'day', INTERVAL '0', false, 'UTC', 'marketing', 'marketing',
   'connector:shopify_admin.orders')
ON CONFLICT (event_type) DO NOTHING;
