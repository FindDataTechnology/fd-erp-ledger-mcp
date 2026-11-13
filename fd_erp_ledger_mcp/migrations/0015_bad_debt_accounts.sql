-- 坏账准备与信用减值损失：通用科目补两个（change: add-bad-debt-provision）。
--
-- 应收账款在报表上应是**净额**（预期能收回的部分），而不是全额。按账龄比例计提坏账准备是
-- 任何行业都成立的做法，差异只在「分档与费率」这些参数上——所以科目进内核科目表，参数留在
-- 模板层的车道配置（同 0002 边界：行业口径不动内核科目）。
--
-- 1231 坏账准备是抵减资产（contra-asset）：报表口径的应收账款净额 = 1100 − 1231。
-- account_type 仍是封闭集（asset/liability/equity/revenue/expense），没有 contra-asset，
-- 故按 asset 登记，靠 note 与金额方向（贷方）表达抵减；取净额必须减 1231，只看 1100 会算高。
--
-- 6602 信用减值损失是期间费用（expense），进利润表与未结转损益。
-- 追加式幂等：ON CONFLICT (code) DO NOTHING；既有租户重跑内核 CLI seed-accounts 即得，
-- 无需任何数据迁移。

INSERT INTO universal_accounts(code, name, name_en, account_type, preset_version, note) VALUES
    ('1231', '坏账准备',     'Allowance for doubtful accounts', 'asset', 'v1',
     '抵减项：报表取应收账款净额须 1100 − 1231，不得只看 1100'),
    ('6602', '信用减值损失', 'Credit impairment loss',          'expense', 'v1',
     '计提坏账准备的对侧费用；减少准备时记贷方（冲回）')
ON CONFLICT (code) DO NOTHING;
