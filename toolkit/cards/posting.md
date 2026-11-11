# posting 组 · 过账层

> 本组工具：`submit_posting_rule` `approve_posting_rule` `list_posting_rules` `post_event` `list_posting_gaps`
> 工具面真源是 `../../tool-surface.json`；本卡是 agent 侧说明书，不是新能力。

## 能做什么

- `submit_posting_rule`：提交过账规则草稿（版本化，不参与过账）。科目必须落在机器车道白名单内，且必须同时含借贷两侧。
- `approve_posting_rule`：审批上线——退役旧 active 版本，本版本生效；升版不重写历史分录。
- `list_posting_rules`：列各版本与状态（`draft` / `active` / `retired`）。
- `post_event`：按生效规则把已接受事件过一次账，同一事件只过一次。缺规则时抛 `POSTING_RULE_MISSING` 并把事件挂入待补清单，账本余额不变（不猜科目）。
- `list_posting_gaps`：缺规则待补清单——哪些事件在等哪条规则（未解除的缺口），带完整性声明。

**什么时候用**：`list_posting_gaps` 先看缺什么 → `submit_posting_rule` 拟草稿 → `approve_posting_rule` 上线 → `post_event` 过账。规则升版只影响之后的事件；历史分录不动。

## 不许做什么

- **不许缺规则时猜科目、套用相近规则或生成零金额占位分录**：缺规则的正确处置是挂待补（`POSTING_RULE_MISSING` + `posting_gaps`），余额不变；猜科目会把「不知道」记成一笔确定的错账。
- **不许程序自审自批过账规则，或让 draft 版本参与过账**：规则须人工审批上线，同一事件类型只允许一条 active；draft 参与过账会让未审的科目映射直接进账。
- **不许改历史分录来「重算」新规则**：升版只影响之后的事件，历史分录 append-only；重写历史会让已出报表无法复现。
- **不许在规则里放白名单外科目的，尤其是资金类科目**（资金池 / 代收代付 / 分账执行）：机器车道科目受 `POSTING_ACCOUNTS` 白名单约束（不含现金/预收/权益），白名单外提交即拒。

## 硬纪律

1. **agent 写路径只出 proposal 或拟稿事件**。本组产的是「规则」与「派生分录」：规则草稿是拟稿，`post_event` 产生的分录严格按 active 规则派生；agent 不能借过账层直接写一笔自定义分录。
2. **未知必须显式 `__unknown__`**。规则引用的载荷字段不存在时 `POSTING_RULE_MISSING` 并点名缺失字段；不要用空串或默认值让规则「跑通」，那会把缺字段伪装成零金额。
3. **余额不可直写**。过账是余额的唯一来源之一，但写法是「追加分录」；没有改余额的入口，冲正也是追加反向分录。
4. **缺规则挂待补**是硬约束不是建议：缺规则时事件进 `posting_gaps`、账本余额不变；补规则后重新 `post_event`，同一事件只过一次。
5. **资金类科目禁入（二清红线）**。过账层是红线最靠近执行的一层：资金池、代收代付、分账执行不得出现在规则或分录里；`POSTING_ACCOUNTS` 白名单就是这条线的机器实现。

## 哪类岗位调哪几件

| 岗位 | 可调工具 | 说明 |
|---|---|---|
| inspector（只读巡检） | `list_posting_rules`、`list_posting_gaps` | 看规则版本与待补积压；不写过账、不批规则。 |
| analyst（定性 / 拟稿） | `submit_posting_rule`、`post_event`、`list_posting_rules`、`list_posting_gaps` | 拟规则草稿、按 active 规则过账；不持审批门。 |
| decider（持确认门） | `approve_posting_rule`、`submit_posting_rule`、`list_posting_rules`、`list_posting_gaps` | 审批上线；拟稿优先由核对岗出，避免同轮自拟自批。 |

确认门只在 `approve_posting_rule`：规则一旦 active，之后所有同类型事件都按它过账，所以审批是最高杠杆的一次点击。
