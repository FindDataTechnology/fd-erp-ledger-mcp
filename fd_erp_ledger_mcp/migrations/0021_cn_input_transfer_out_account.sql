-- 进项税额转出科目：通用科目补一个（change: add-cn-red-letter-and-input-transfer-out）。
--
-- 已抵扣的进项在用于集体福利/个人消费、发生非正常损失、用于免税或简易计税项目时，
-- 必须从进项里**转出**（增加应纳增值税）。没有这个科目，「转出」只能挤进别的应交税费明细，
-- 应纳增值税的公式就没法写成「销项 − 进项 + 进项转出」，附加税的基数跟着错。
--
-- 科目码取自 `fd-erp-vertical-templates/localization/cn` 数据包（Odoo l10n_cn @20.0）实测：
-- 2221.01.04 应交税费 - 应交增值税（进项税额转出）。
--
-- 追加式幂等：ON CONFLICT (code) DO NOTHING；既有租户重跑内核 CLI seed-accounts 即得。

INSERT INTO universal_accounts(code, name, name_en, account_type, preset_version, note) VALUES
    ('2221.01.04', '应交税费-应交增值税（进项税额转出）',
     'VAT payable - input VAT transferred out', 'liability', 'v1',
     '贷方增加应纳增值税；用于集体福利/非正常损失/免税或简易计税项目')
ON CONFLICT (code) DO NOTHING;
