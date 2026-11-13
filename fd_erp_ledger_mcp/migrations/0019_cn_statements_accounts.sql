-- 国别报表科目：通用科目补四个（change: add-cn-localization-pack）。
--
-- 小企业准则的利润表有「税金及附加」「营业外收入」「营业外支出」「所得税费用」四个项目。
-- 内核没有对应科目时，这张表出不真：所得税无处可记，净利润永远等于利润总额；
-- 税金及附加只能挤进「其他经营费用」，营业外收支只能挤进收入/成本——项目口径全错。
--
-- 科目码直接用准则码（5301/5403/5711/5801），不存在内核码与准则码不同名的歧义，
-- 映射表里一一对上即可。
--
-- 6602 信用减值损失仍单独保留：它是我们的计量口径（按账龄计提），准则里没有这个科目，
-- 报表上归到「营业外支出」（映射 6602→5711），与真正的营业外支出并列相加。
--
-- 追加式幂等：ON CONFLICT (code) DO NOTHING；既有租户重跑内核 CLI seed-accounts 即得。

INSERT INTO universal_accounts(code, name, name_en, account_type, preset_version, note) VALUES
    ('5301', '营业外收入', 'Non-operating income', 'revenue', 'v1',
     '与经营无关的利得（政府补助、盘盈利得等）；利润表单列「加：营业外收入」'),
    ('5403', '税金及附加', 'Taxes and surcharges', 'expense', 'v1',
     '消费税、城建税、教育费附加、印花税等；利润表单列，不混进「其他经营费用」'),
    ('5711', '营业外支出', 'Non-operating expense', 'expense', 'v1',
     '与经营无关的损失（坏账损失、非常损失、滞纳金等）；利润表单列'),
    ('5801', '所得税费用', 'Income tax expense', 'expense', 'v1',
     '当期所得税费用；利润表最后一步，净利润 = 利润总额 − 所得税费用')
ON CONFLICT (code) DO NOTHING;
