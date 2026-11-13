# ERP 内核工具书（agent 侧）

本工具书是 agent 侧说明书，不是新能力；工具面真源是 `../tool-surface.json`（37 件 / 9 组）。本目录只回答：这组工具能做什么、不许做什么、硬纪律是什么、哪类岗位能碰哪几件。

- 机器可读锚：`catalog.json`（组 → 工具 → 允许岗位 → 纪律 id → forbiddens）。闸 `scripts/check_toolkit.py` 用它与工具面做双向核对。
- 人读正文：`cards/<group_id>.md`，一组一张卡。
- 本文件：32 工具 → 8 组 → 岗位 / 团队覆盖一览。

## 三条岗位线（工具权限边界）

| 岗位 | 能做什么 | 写边界 |
|---|---|---|
| `inspector`（只读巡检） | 取数、看队列、跑对账任务、试算 | 不写：不提交、不确认、不占用、不投影 |
| `analyst`（定性 / 拟稿） | 定性差异、出 proposal、拟稿事件、拟规则草稿、执行派生动作（计费 / 过账 / 投影 / 红冲） | 只出 proposal 或拟稿事件，不持确认门 |
| `decider`（持确认门） | `confirm_proposal`、`decide_event`、`approve_billing_rule`、`approve_posting_rule` | 唯一能把拟稿变成事实的岗位 |

确认门四件工具（`confirm_proposal` / `decide_event` / `approve_billing_rule` / `approve_posting_rule`）只标 `decider`；写类工具不标 `inspector`。精确到工具的授权见下表与 `catalog.json.groups[].tool_roles`。

## 工具 → 组 → 岗位 / 团队

| 工具 | 组 | 车道 | 可调岗位 | 典型承担团队 |
|---|---|---|---|---|
| `submit_proposal` | proposal | 拟稿 | analyst | erp-payables-fleet / erp-order-to-cash-fleet |
| `list_proposals` | proposal | 读 | inspector / analyst / decider | 全体（待办视图） |
| `confirm_proposal` | proposal | 确认门 | decider | erp-payables-fleet / erp-order-to-cash-fleet |
| `reject_proposal` | proposal | 确认门（否决） | decider | erp-payables-fleet / erp-order-to-cash-fleet |
| `query_balance` | ledger | 读 | inspector / analyst / decider | erp-payables-fleet |
| `query_entries` | ledger | 读 | inspector / analyst / decider | erp-payables-fleet / erp-invoice-fleet |
| `run_reconciliation` | recon | 长任务（零外部写入） | inspector / analyst / decider | erp-invoice-fleet / erp-payables-fleet |
| `get_recon_report` | recon | 读 | inspector / analyst / decider | erp-invoice-fleet / erp-payables-fleet |
| `submit_billing_rule` | billing | 拟稿 | analyst / decider | erp-billing-fleet |
| `approve_billing_rule` | billing | 确认门 | decider | erp-billing-fleet |
| `list_billing_rules` | billing | 读 | inspector / analyst / decider | erp-billing-fleet |
| `bill_event` | billing | 派生写（产机器车道 proposal） | analyst | erp-billing-fleet |
| `list_fees` | billing | 读 | inspector / analyst / decider | erp-billing-fleet |
| `get_fee_detail` | billing | 读 | inspector / analyst / decider | erp-billing-fleet |
| `reverse_fee` | billing | 纠错（红冲） | analyst / decider | erp-billing-fleet |
| `simulate_billing` | billing | 读（零写入试算） | inspector / analyst / decider | erp-billing-fleet |
| `bill_period_snapshot` | billing | 派生写（期间快照） | analyst | erp-billing-fleet |
| `reserve_credit` | credit | 备忘写 | analyst | erp-credit-fleet |
| `release_credit` | credit | 备忘写 | analyst / decider | erp-credit-fleet |
| `query_credit` | credit | 读 | inspector / analyst / decider | erp-credit-fleet / erp-order-to-cash-fleet |
| `list_credit_reservations` | credit | 读 | inspector / analyst / decider | erp-credit-fleet |
| `ingest_event` | event | 导入（写入即事实） | analyst | erp-fact-intake-fleet / erp-invoice-fleet |
| `submit_event` | event | 拟稿 | analyst | erp-fact-intake-fleet / erp-order-to-cash-fleet |
| `decide_event` | event | 确认门 | decider | erp-fact-intake-fleet / erp-invoice-fleet |
| `query_events` | event | 读 | inspector / analyst / decider | erp-fact-intake-fleet 及全体 |
| `query_object_balance` | object | 读 | inspector / analyst / decider | erp-fact-intake-fleet |
| `project_event` | object | 投影写 | analyst | erp-fact-intake-fleet |
| `submit_posting_rule` | posting | 拟稿 | analyst / decider | erp-posting-fleet |
| `approve_posting_rule` | posting | 确认门 | decider | erp-posting-fleet |
| `list_posting_rules` | posting | 读 | inspector / analyst / decider | erp-posting-fleet |
| `post_event` | posting | 派生写（过账） | analyst | erp-posting-fleet / erp-payables-fleet |
| `list_posting_gaps` | posting | 读 | inspector / analyst / decider | erp-posting-fleet |
| `close_period` | period | 确认门（关账） | decider | erp-payables-fleet / erp-posting-fleet |
| `reopen_period` | period | 确认门（重开） | decider | erp-payables-fleet / erp-posting-fleet |
| `period_status` | period | 读 | inspector / analyst / decider | erp-payables-fleet |
| `period_report` | period | 读（快照核对） | inspector / analyst / decider | erp-payables-fleet |
| `list_periods` | period | 读 | inspector / analyst / decider | erp-payables-fleet |

合计 37 行 = `tool-surface.json` 37 件，逐名相等（缺一行、多一行都会被闸判红）。

## 团队覆盖说明

上面「典型承担团队」是《erp-agent-teams》七个通用棋盘团队与本工具书的对齐口径，用来回答「哪件工具在哪个队里被谁调」：

- `erp-payables-fleet`：应收应付 + 多币种 + 期末余额核对（proposal / ledger / recon / posting 的只读与过账）
- `erp-billing-fleet`：计费规则与费用明细（billing 全组）
- `erp-credit-fleet`：额度占用 / 释放 / 孤儿占用（credit 全组）
- `erp-fact-intake-fleet`：事件导入与拟稿裁决 + 数量面（event / object / inventory）
- `erp-posting-fleet`：过账规则治理与待补清单清理（posting 全组）
- `erp-invoice-fleet`：发票事件登记与对账（event 的导入/确认 + recon）
- `erp-order-to-cash-fleet`：订单 → 采购 → 主数据（proposal / credit 的只读与拟稿）

团队命名与编制不在本仓交付；这里只登记「工具属于哪条业务线」，供 `fleet-packs` 侧逐岗 `mcpTools` 收窄时引用。跨仓声明缺失不阻断本仓 CI（单仓闸只判工具面 ↔ 工具书两方一致）。

## 五条判据（`scripts/check_toolkit.py`）

1. `catalog.json` 的工具并集与 `tool-surface.json` 双向相等（少一件 / 多一件都红，并点名）；组内工具与工具面分组逐组对齐。
2. 卡内点名的工具必须是工具面里存在的符号（防改名 / 下线 / 笔误后静默失效）。
3. 每件工具至少被一张卡点名（缺书即红），且本 README 覆盖表逐行覆盖 32 件。
4. 每张卡都有「能做什么 / 不许做什么 / 硬纪律 / 哪类岗位调哪几件」四段，且声明的每条纪律锚句都在正文里。
5. 资金类科目（资金池 / 代收代付 / 分账执行）不得出现在可写口径：句内无否定词即红。

退出码：`0` 通过 / `1` 阻断 / `2` 出处缺失（工具面、catalog、卡目录缺一即 2）。

## 改这个目录的规矩

- 工具面新增/删除工具时，先改 `tool-surface.json`（真源），再补 `catalog.json` 与对应卡；闸会点名缺的卡与缺的工具。
- 硬纪律不是建议：`catalog.json.disciplines` 的 `anchor` 是闸的判据，改措辞要同步改卡，否则卡会被判「缺纪律」。
- 不要在本目录写实现细节（表名、函数名、行号）作为判据；符号级引用才可长期维护。
