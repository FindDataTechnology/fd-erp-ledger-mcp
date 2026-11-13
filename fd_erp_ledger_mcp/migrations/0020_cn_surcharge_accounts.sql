-- 附加税与所得税科目：通用科目补四个（change: add-cn-surcharge-and-income-tax）。
--
-- 一张月度税表不止增值税：城建税与两项教育费附加以**应纳增值税**为计税依据，企业所得税按季预缴。
-- 没有这四个科目，附加税只能挤进「其他经营费用」（口径错，报表项目对不上），所得税无处可挂
-- （净利润算不对）。
--
-- 科目码取自 `fd-erp-vertical-templates/localization/cn` 数据包（Odoo l10n_cn @20.0）实测：
-- 2221.11 企业所得税 / 2221.12 城市维护建设税 / 2221.13 教育费附加 / 2221.14 地方教育费附加。
--
-- 追加式幂等：ON CONFLICT (code) DO NOTHING；既有租户重跑内核 CLI seed-accounts 即得。

INSERT INTO universal_accounts(code, name, name_en, account_type, preset_version, note) VALUES
    ('2221.11', '应交税费-应交企业所得税', 'Corporate income tax payable', 'liability', 'v1',
     '按季预缴；本期应预缴 = 本年累计利润 × 税率 − 已预缴累计'),
    ('2221.12', '应交税费-应交城市维护建设税', 'Urban maintenance and construction tax payable',
     'liability', 'v1', '以应纳增值税为基数，税率 7%（市区）/5%（县城镇）/1%（其他）'),
    ('2221.13', '应交税费-应交教育费附加', 'Education surcharge payable', 'liability', 'v1',
     '以应纳增值税为基数，法定 3%'),
    ('2221.14', '应交税费-应交地方教育附加', 'Local education surcharge payable', 'liability',
     'v1', '以应纳增值税为基数，法定 2%')
ON CONFLICT (code) DO NOTHING;
