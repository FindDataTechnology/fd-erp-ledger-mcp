-- 生产成本（在制品）：通用科目补一个（change: add-manufacturing-orders）。
--
-- 有生产的客户，领料与完工之间压着一笔在制品。没有这个科目，领料只能记进 1200 存货
-- （存货凭空变多）或 5000 营业成本（未完工就确认成本），两种都会让资产负债表与毛利失真。
--
-- 1401 是资产（asset）：借 = 领料归集，贷 = 完工转出；余额即期末在制品。
-- 它不进 1601/1602 那套抵减逻辑，也不是期间费用。
--
-- 追加式幂等：ON CONFLICT (code) DO NOTHING；既有租户重跑内核 CLI seed-accounts 即得，
-- 无需任何数据迁移。

INSERT INTO universal_accounts(code, name, name_en, account_type, preset_version, note) VALUES
    ('1401', '生产成本', 'Work in process', 'asset', 'v1',
     '在制品：领料借记归集、完工贷记转出；余额=期末在制品，属存货类资产')
ON CONFLICT (code) DO NOTHING;
