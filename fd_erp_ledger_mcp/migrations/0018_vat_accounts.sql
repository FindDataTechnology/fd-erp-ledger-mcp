-- 增值税科目树：通用科目补六个（change: add-cn-localization-pack）。
--
-- 没有税费科目的账，记不了增值税：销项只能挤进收入（收入虚增）、进项只能挤进成本
-- （成本虚增），两处同时错，而利润表「看起来正常」。
--
-- 科目码取自 `fd-erp-vertical-templates/localization/cn` 数据包（Odoo l10n_cn @20.0）实测，
-- 不是照记忆写的——`2221.01.05` 是**出口退税**，转出未交增值税是 `2221.01.07`。
-- 层级用点号科目码表达（1002.01 同款约定），内核仍是扁平 code，不引入父子列。
--
-- 本迁移只补增值税主链六个；出口退税、加计抵减、简易计税等仍在数据包里、未启用
-- （要做到即加科目 + 加过账规则，不在本 change 边界内）。
--
-- 追加式幂等：ON CONFLICT (code) DO NOTHING；既有租户重跑内核 CLI seed-accounts 即得，
-- 无需任何数据迁移。

INSERT INTO universal_accounts(code, name, name_en, account_type, preset_version, note) VALUES
    ('2221', '应交税费', 'Taxes payable', 'liability', 'v1',
     '税费总括科目；法定报表「应交税费」项目取自此科目子树'),
    ('2221.01', '应交税费-应交增值税', 'VAT payable', 'liability', 'v1',
     '增值税归集：销项贷记、进项借记；月末把差额转出到 2221.01.07'),
    ('2221.01.01', '应交税费-应交增值税（销项税额）', 'Output VAT', 'liability', 'v1',
     '销售时按税率计提的销项税额（贷方）'),
    ('2221.01.02', '应交税费-应交增值税（进项税额）', 'Input VAT', 'liability', 'v1',
     '取得专票等可抵扣的进项税额（借方）'),
    ('2221.01.07', '应交税费-应交增值税（转出未交增值税）', 'VAT transferred out',
     'liability', 'v1',
     '月末把「销项−进项」转出到 2221.02；本迁移原写 2221.01.05，实测那是出口退税，已更正'),
    ('2221.02', '应交税费-未交增值税', 'VAT unpaid', 'liability', 'v1',
     '月末结转后待缴的增值税；缴纳时借记本科目')
ON CONFLICT (code) DO NOTHING;
