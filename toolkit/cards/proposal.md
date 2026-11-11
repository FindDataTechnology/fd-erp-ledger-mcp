# proposal 组 · 拟落账事件车道

> 本组工具：`submit_proposal` `list_proposals` `confirm_proposal` `reject_proposal`
> 工具面真源是 `../../tool-surface.json`；本卡是 agent 侧说明书，不是新能力。

## 能做什么

- `submit_proposal`：把「业务事实已发生、账上还该记一笔」的判断写成 proposal（金额、科目、币种、出处、来源引用）。它只创建 proposals 行，**不落账**；同 `stable_id` 重复提交返回首次结果。
- `list_proposals`：分页查本租户 proposal 与状态（待确认 / 已确认 / 已拒绝），给巡检与核对一个共同的待办视图。
- `confirm_proposal`：确认门。确认后按复式分录落账，借贷必平由 DB 约束强制（不平整笔 `UNBALANCED`），同 `stable_id` 幂等。
- `reject_proposal`：拒绝并留审计痕，不落账。拒绝不是删除，历史仍在。

**什么时候用**：核对岗发现差异且能指出「哪笔事实、记哪个科目、金额与币种从哪来」时出 proposal；拍板岗逐笔看证据后 confirm 或 reject。外部系统已经给出原始事实的，走 event 组的 `ingest_event`，不要在这里补一笔。

## 不许做什么

- **不许把 proposal 当账**：proposal 是拟稿，落账唯一入口是确认门。绕过 `confirm_proposal` 直接找分录写法，等于 agent 自己既当提案人又当记账人。
- **不许用 proposal 修改或冲销已落账分录**：分录 append-only，更正只走冲正（反向分录 / 冲正 proposal）；用新 proposal「盖掉」旧账会让余额与证据链对不上。
- **不许用空串、0 或空数组补缺失的金额、币种、出处**：空值会被读成「没有」而不是「不知道」，落账后无法与真实的零金额区分，冲正时也找不到依据。
- **不许把 `reject_proposal` 当删除**：拒绝只改状态；换个 `stable_id` 重提同一笔会制造两条互相矛盾的判断。
- **不许在 proposal 里写资金类科目**（资金池 / 代收代付 / 分账执行）：这类科目连拟稿都不该出现，否则确认门会把它落成账。

## 硬纪律

1. **agent 写路径只出 proposal 或拟稿事件**。内核没有给 agent 的直写账本工具；proposal 经 `confirm_proposal` 才成为账，拟稿事件经 `decide_event` 才成为事实。把拟稿读成事实，人就失去了「这批账是谁的判断」的追溯点。
2. **未知必须显式 `__unknown__`**。空串 / 0 / 空数组与「确实没有」在存储和读回上都不可区分；下游会把「未查」读成「没有」。确实不知道的字段写 `__unknown__`，宁可让核对卡住，也不要让错值静默通过。
3. **余额不可直写**。本组没有余额字段可改，余额永远由分录重算；proposal 的作用是让正确分录被确认，而不是调整某个余额数字。
4. **过账须已审批规则，缺规则挂待补**。确认门只决定「这笔事实成不成立」，不决定「记哪个科目」；科目映射由过账层的已审批规则派生，缺规则时事件进待补清单、余额不变，不许在 proposal 里猜科目兜底。
5. **资金类科目禁入（二清红线）**。资金池、代收代付、分账执行不属本内核；资金走客户自选持牌通道，本组只做拟稿与确认，不做资金归集。

## 哪类岗位调哪几件

| 岗位 | 可调工具 | 说明 |
|---|---|---|
| inspector（只读巡检） | `list_proposals` | 只看待确认/已拒绝队列与积压，不提交、不确认。 |
| analyst（定性 / 拟稿） | `submit_proposal`、`list_proposals` | 出 proposal 并维护证据；不持确认门。 |
| decider（持确认门） | `confirm_proposal`、`reject_proposal`、`list_proposals` | 逐笔确认或拒绝；拒绝也留痕。 |

岗位边界就是权限边界：同一岗位既拟稿又确认时，确认门退化成一次点击。
