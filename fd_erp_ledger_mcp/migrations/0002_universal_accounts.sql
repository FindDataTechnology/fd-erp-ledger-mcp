-- 通用科目最小集（erp-vertical-templates 任务 1.1 / design D2）。
-- 行业无关的内核科目底座：五个类别（资产/负债/权益/收入/费用）各最小组。
-- 行业专属科目 MUST NOT 注入本表或 accounts：行业口径一律走科目映射 pack
-- （呈现层重算，不动账本事实）。
-- 幂等：全部 CREATE IF NOT EXISTS + ON CONFLICT DO NOTHING，可重放。

-- 内核级科目预设（租户无关；code 是全平台稳定查询口径）。
CREATE TABLE IF NOT EXISTS universal_accounts (
    code          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    name_en       TEXT NOT NULL,
    account_type  TEXT NOT NULL CHECK (account_type IN ('asset','liability','equity','revenue','expense')),
    preset_version TEXT NOT NULL,
    note          TEXT
);

INSERT INTO universal_accounts(code, name, name_en, account_type, preset_version, note) VALUES
    ('1000', '库存现金及银行存款', 'Cash and bank',                   'asset',     'v1', '货币资金底座'),
    ('1100', '应收账款',           'Accounts receivable',             'asset',     'v1', '平台/渠道未结算应收的对账承载体'),
    ('1200', '存货',               'Inventory',                       'asset',     'v1', '商品库存（跨境/餐饮通用）'),
    ('1300', '在途结算款',         'Settlements in transit',          'asset',     'v1', '渠道/平台已确认未到账的在途资金'),
    ('2000', '应付账款',           'Accounts payable',                'liability', 'v1', '供应商与平台应付'),
    ('2100', '预收及暂收款',       'Advances and temporary receipts', 'liability', 'v1', '预收/代收暂挂（不含资金执行）'),
    ('3000', '业主投入资本',       'Owner contributed capital',       'equity',    'v1', '权益底座'),
    ('3100', '留存收益',           'Retained earnings',               'equity',    'v1', '权益底座'),
    ('4000', '营业收入',           'Operating revenue',               'revenue',   'v1', '收入唯一内核科目；行业收入细目走映射'),
    ('5000', '营业成本',           'Cost of sales',                   'expense',   'v1', '成本底座'),
    ('5100', '平台及支付费用',     'Platform and payment fees',       'expense',   'v1', '佣金/支付手续费/平台费用独立维度'),
    ('5200', '其他经营费用',       'Other operating expenses',        'expense',   'v1', '其余期间费用兜底')
ON CONFLICT (code) DO NOTHING;

-- 逐租户种子函数：把通用科目最小集写进 accounts（幂等，可重复调用）。
-- 调用面 = 内核 CLI seed-accounts（运维动作），模板层 MUST NOT 直连库表，
-- 只能经内核工具面使用这些科目编码。
CREATE OR REPLACE FUNCTION erp_seed_universal_accounts(p_tenant text) RETURNS int AS $$
DECLARE
    v_count int;
BEGIN
    INSERT INTO accounts(tenant_id, code, name, account_type, currency)
    SELECT p_tenant, u.code, u.name, u.account_type, 'CNY'
    FROM universal_accounts u
    ON CONFLICT (tenant_id, code) DO NOTHING;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;
