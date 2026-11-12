-- 分析维度（erp-analytical-dimensions / design D1-D5）。
-- 让账本不只按科目切：维度挂在提案上，确认后逐行带进分录；冲正继承原维度。
--
-- 为什么键是封闭集、值是自由字符串：维度键是「账本按什么切」，与 account_type 同级，属于
-- schema；值是客户的组织/项目编码，属于数据。键封闭让 `dept` / `department` 这类拼写漂移
-- 无法悄悄把同一部门的实际额拆成两半（那种错不会报错，只会算错）。
--
-- 为什么不回填历史：给既有分录补维度只能靠猜，猜出来的归属比没有归属更糟——报表会「看起来
-- 完整」而实际是编的。既有行保持空集 `{}`，读取方必须自己区分「该维度没有发生额」与
-- 「发生额没标注维度」。
--
-- 幂等：ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS，可重放。

ALTER TABLE proposals
    ADD COLUMN IF NOT EXISTS dimensions JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE journal_entries
    ADD COLUMN IF NOT EXISTS dimensions JSONB NOT NULL DEFAULT '{}'::jsonb;

-- 过滤用包含语义（`dimensions @> '{"department":"D-1"}'`），jsonb_path_ops 对它最省。
CREATE INDEX IF NOT EXISTS journal_entries_dimensions_idx
    ON journal_entries USING GIN (dimensions jsonb_path_ops);

CREATE INDEX IF NOT EXISTS proposals_dimensions_idx
    ON proposals USING GIN (dimensions jsonb_path_ops);
