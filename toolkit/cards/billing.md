# billing 组 · 计费规则引擎

> 本组工具：`submit_billing_rule` `approve_billing_rule` `list_billing_rules` `bill_event` `list_fees` `get_fee_detail` `reverse_fee` `simulate_billing` `bill_period_snapshot`
> 工具面真源是 `../../tool-surface.json`；本卡是 agent 侧说明书，不是新能力。

## 能做什么

- `submit_billing_rule`：登记计费规则新版本（`draft → pending_approval`）。同 `rule_key` 版本递增；无出处费率拒绝（`RATE_UNSOURCED`）。
- `approve_billing_rule`：规则确认门（`pending_approval → active`，人工确认，不接受程序自审自批）；同维度冲突未处置（`RULE_CONFLICT`）、收费项覆盖缺失拦截上线；旧 active 版本随新版本退役。
- `list_billing_rules`：分页查规则（版本、状态机、config、`rate_source`）。
- `bill_event`：按 active 规则对单个事件计费，写 `fee_details`（append-only，快照锁定规则版本）并组一条 billing-engine 机器车道 proposal——**不直接落账**。
- `list_fees` / `get_fee_detail`：查费用明细（含红冲反向行与 `reversal_of`）与四层溯源（费用项 → 业务单据 → 作业事件 → 计费规则版本），并可按快照确定性重算。
- `reverse_fee`：红冲费用——新增反向明细行（原行原样保留），原分录已落账时组冲正 proposal。
- `simulate_billing`：按历史事件集试算新旧规则费用差异，零写入。
- `bill_period_snapshot`：按期间触发日终快照计费（存储费 = 日终库存快照 × 费率）；幂等键 = (租户, 期间, `stable_id`)，同期间重跑返回首次结果。

**什么时候用**：费用核对先 `get_fee_detail` 看溯源与重算；规则要改先 `simulate_billing` 试算，再 `submit_billing_rule` → `approve_billing_rule`；按事件计费 `bill_event`，按期间 `bill_period_snapshot`；记错了 `reverse_fee` 红冲，不删原行。

## 不许做什么

- **不许跳过审批让新费率生效，或程序自审自批 `approve_billing_rule`**：规则必须 `pending_approval → active`，人工确认是唯一上线入口；自审自批会让「谁批准的」失去可追溯性，冲突拦截也失去意义。
- **不许用无出处的费率**：`rate_source` 缺失或空白一律 `RATE_UNSOURCED` 拒绝。费率必须指回合同 / 报价 / 协议，否则费用无法复核。
- **不许手改 `fee_details` 或删除原费用行**：费用明细 append-only；纠错只能 `reverse_fee` 生成反向行（`reversal_of` 关联，原行原样保留），删改会同时抹掉四层溯源。
- **不许把计费结果直接落账**：计费落账走 billing-engine 机器车道 proposal，仍需确认门；直接写分录会绕开科目白名单与借贷平衡约束。
- **不许在计费科目或费率口径里引入资金类科目**（资金池 / 代收代付 / 分账执行）：内核只算服务费与存储费，不代收代付、不分账执行。

## 硬纪律

1. **agent 写路径只出 proposal 或拟稿事件**。`bill_event` / `bill_period_snapshot` / `reverse_fee` 产生的都是费用明细与机器车道 proposal，不是已落账分录；确认门仍在人手里。
2. **未知必须显式 `__unknown__`**。汇率、费率、收费项、快照来源任一缺失都要标未知并拒绝计费；用 0 补缺会算出「零元费用」，比报错更难发现。
3. **余额不可直写**。费用不动余额字段；余额只由已确认分录重算。红冲也不是改余额，是补反向分录。
4. **过账须已审批规则，缺规则挂待补**。计费规则（费率）与过账规则（科目映射）是两件事：费率 active 不等于科目映射存在；费用 proposal 落账仍要过账规则，缺则挂待补。
5. **资金类科目禁入（二清红线）**。资金池、代收代付、分账执行不参与计费，也不作为费率基数或科目；资金走客户自选持牌通道。计费只回答「该收多少服务费」。

## 哪类岗位调哪几件

| 岗位 | 可调工具 | 说明 |
|---|---|---|
| inspector（只读巡检） | `list_billing_rules`、`list_fees`、`get_fee_detail`、`simulate_billing` | 巡检只看规则状态、费用与试算差异，不产费用、不批规则。 |
| analyst（定性 / 拟稿） | `submit_billing_rule`、`bill_event`、`list_fees`、`get_fee_detail`、`reverse_fee`、`simulate_billing`、`bill_period_snapshot`、`list_billing_rules` | 拟规则草稿、跑计费、红冲纠错；不持审批门。 |
| decider（持确认门） | `approve_billing_rule`、`reverse_fee`、`list_billing_rules`、`list_fees`、`get_fee_detail`、`simulate_billing` | 审批上线与重大红冲；审批前先看试算与冲突。 |

`submit_billing_rule` 与 `approve_billing_rule` 分属两岗：同一轮里由同一人既拟稿又批准，等于自审自批，团队编排必须错岗。
