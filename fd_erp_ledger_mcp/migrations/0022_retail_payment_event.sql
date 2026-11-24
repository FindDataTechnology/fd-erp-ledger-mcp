-- 零售预付到账事实（change: establish-ai-native-ecommerce-stack 0.10）。
--
-- 与既有 `payment_received`（B2B 回款，requires_posting=true）**不是一件事**：
--   · payment_received        = 客户回款，冲应收，要过账；
--   · retail_payment_received = 零售买家付款到**商家自有账户**，平台不经手、不留存、
--                               不冲任何应收（零售没有应收账款这个对象），因此
--                               requires_posting=false。
-- 把两者并成一个类型会逼出一个「过不过账看载荷」的分支，而事件类型的 requires_posting
-- 是注册表级的单一事实——一条类型两种过账语义，等于没有语义。
--
-- 幂等键走既有 `stable_id`；同一幂等键重复登记由内核去重，不新增事实。

INSERT INTO event_types
    (event_type, description, payload_schema, finality_granularity,
     revision_window, requires_posting)
VALUES
    ('retail_payment_received',
     '零售收款到账（钱进商家自有账户；平台只记事实，不落分录、不经手资金）',
     '{"required":["order_ref","amount","currency","proof_ref"],
       "properties":["order_ref","amount","currency","proof_ref","paid_at","channel","buyer_ref"]}'::jsonb,
     'day', INTERVAL '0', false)
ON CONFLICT (event_type) DO NOTHING;
