# credit 组 · 信用额度

> 本组工具：`reserve_credit` `release_credit` `query_credit` `list_credit_reservations`
> 工具面真源是 `../../tool-surface.json`；本卡是 agent 侧说明书，不是新能力。

## 能做什么

- `query_credit`：查额度位——`limit`、`ar_balance`（1300 应收余额，由分录确定性重算）、`reserved`、`available = limit − ar_balance − reserved`。
- `reserve_credit`：占用额度（备忘性质，不写复式分录）；不足时 `CREDIT_EXCEEDED` 并带额度明细；同 `stable_id` 幂等。
- `release_credit`：释放占用（`reserved → released`，幂等）；推进的是备忘表状态，不是账本事实。
- `list_credit_reservations`：列占用明细与派生状态（`reserved` / `expired` / `released`）和 `expires_at`；只读，不释放、不修改任何占用。

**什么时候用**：接单/发货前看 `available` 决定能不能接；需要锁额度时 `reserve_credit`；订单关闭、取消、逾期清理时 `release_credit`。巡检用 `list_credit_reservations` 找孤儿占用（已过期或对应订单已关闭仍 reserved），只出清理建议，拍板岗才释放。

## 不许做什么

- **不许把信用占用当账本事实、或给它写复式分录**：占用是备忘性质，不进账本。额度口径由分录派生（limit − 1300 应收 − 占用），给占用记账会让额度被重复扣减。
- **不许忽略 `available` 硬占，或先占用后补授权**：额度不足一律 `CREDIT_EXCEEDED` 并带明细；硬占会把 `available` 压成负数，后续订单全部被错误拒绝。
- **不许手工把占用状态改成 `released` 而不留释放依据**：状态推进要能回答「为什么释放」；无依据释放等于凭空放大可用额度。
- **不许把额度当授信放款 / 扣款通道**：内核只做额度备忘与核对，不执行资金放款或扣款。

## 硬纪律

1. **agent 写路径只出 proposal 或拟稿事件**。本组的写是备忘状态推进（占用/释放），不产分录、不改余额；要动账仍然走 proposal 或拟稿事件。
2. **未知必须显式 `__unknown__`**。客户号、订单号、到期时点缺失时标未知；用空串占位会让占用挂在一个不存在的对象上，孤儿清理时找不到主人。
3. **余额不可直写**。`query_credit` 的 `available` 是派生值，`ar_balance` 来自 1300 分录；不能为了「看起来够额度」去改任一字段。
4. **过账须已审批规则，缺规则挂待补**。额度与过账是两层：占用成立不等于应收已入账；1300 余额只由已审批规则产生的分录重算，缺规则时余额不变。
5. **资金类科目禁入（二清红线）**。信用额度不是资金池，也不做代收代付、分账执行；资金走客户自选持牌通道，内核不因信用场景承接资金动作。

## 哪类岗位调哪几件

| 岗位 | 可调工具 | 说明 |
|---|---|---|
| inspector（只读巡检） | `query_credit`、`list_credit_reservations` | 看额度位与占用明细、找孤儿；不占用、不释放。 |
| analyst（定性 / 拟稿） | `reserve_credit`、`release_credit`、`query_credit`、`list_credit_reservations` | 按订单占用与释放；释放须写明依据。 |
| decider（持确认门） | `release_credit`、`query_credit`、`list_credit_reservations` | 拍板重大/异常占用释放；释放动作要有依据。 |

`reserve_credit` 不出现在巡检岗：占用会直接压缩 `available`，是写动作，不是取数。
