-- 固定资产与累计折旧：通用科目补两个（折旧是通用会计概念，不是行业口径）。
-- 折旧这件事不随行业变：任何行业购置固定资产都要按原值入账、按期计提折旧，
-- 所以 1601/1602 属于内核科目表，而不是某个行业的科目映射 pack。
-- 行业差异只在「资产类别 / 折旧年限 / 残值率」这些参数上，一律走模板层映射包，
-- MUST NOT 注入本表或 accounts（同 0002 的边界：行业口径不动内核科目）。
-- 1602 累计折旧是抵减资产（contra-asset）：报表口径的固定资产净值 = 1601 − 1602。
-- 但 account_type 是封闭集（asset/liability/equity/revenue/expense），没有
-- contra-asset，故按 asset 登记，靠 note 与金额方向（贷方）表达抵减语义；
-- 取净值时必须减 1602，只看 1601 会把净值算高。
-- 追加式幂等：ON CONFLICT (code) DO NOTHING，既有租户重跑内核 CLI seed-accounts
-- 即得这两个科目，无需任何数据迁移。

INSERT INTO universal_accounts(code, name, name_en, account_type, preset_version, note) VALUES
    ('1601', '固定资产',   'Fixed assets',             'asset', 'v1', '购置/自建固定资产原值（折旧计提的借方对象）'),
    ('1602', '累计折旧',   'Accumulated depreciation', 'asset', 'v1', '抵减项：报表取固定资产净值须 1601 − 1602，不得只看 1601')
ON CONFLICT (code) DO NOTHING;
