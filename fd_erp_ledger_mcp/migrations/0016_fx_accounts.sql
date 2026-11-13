-- 汇兑损益：通用科目补一个（change: add-fx-revaluation）。
--
-- 外币货币性项目必须按期末汇率重述：USD 1000 的应收按 7.00 记了 7000，期末汇率 7.20 就该
-- 记 7200，差 200 是汇兑损益，不是「账错了」。科目进内核科目表，**期末汇率留在模板层**
-- 人工登记（外部事实，同 0002 边界：行业参数不进内核）。
--
-- 6603 是期间费用（expense），进利润表与未结转损益，也进期末结转的结平对象。
-- 它 MUST NOT 混进 5200 其他经营费用：混了经营费用口径就失真（汇兑是财务性损益）。
--
-- 追加式幂等：ON CONFLICT (code) DO NOTHING；既有租户重跑内核 CLI seed-accounts 即得，
-- 无需任何数据迁移。

INSERT INTO universal_accounts(code, name, name_en, account_type, preset_version, note) VALUES
    ('6603', '汇兑损益', 'Foreign exchange gain or loss', 'expense', 'v1',
     '期末外币货币性项目按期末汇率重述的差额；损失记借方、收益记贷方')
ON CONFLICT (code) DO NOTHING;
