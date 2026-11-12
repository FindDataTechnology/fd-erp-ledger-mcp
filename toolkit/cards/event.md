# event 组 · 业务事件层

> 本组工具：`ingest_event` `submit_event` `decide_event` `query_events`
> 工具面真源是 `../../tool-surface.json`；本卡是 agent 侧说明书，不是新能力。

## 能做什么

- `ingest_event`：导入车道——写入来自外部系统的原始事实，写入即成为事实（`accepted`）。同 (租户, 车道, `stable_id`) 幂等。适用于 WMS/TMS/渠道等来源系统已经确认过的入库、出库、发运、签收。
- `submit_event`：拟稿车道——agent 产出的事件落为 `pending`，未过确认门不算事实，不进对象投影、不过账。
- `decide_event`：确认门——把 `pending` 的拟稿事件置 `accepted` 或 `rejected`，是唯一改变事件状态的人工入口；ingest 车道事件不需要确认。
- `query_events`：查事件并返回完整性声明（`declared_total` / `returned` / `truncated` / `watermark`）。`truncated` 为真时不得把结果当成全部。

**什么时候用**：外部系统给原始事实 → `ingest_event`；agent 从单据/邮件/对话里整理出事实 → `submit_event` 拟稿，等 `decide_event`。事件定型后不可改写，更正只走冲正；定型前修订走 revision 通道并保留旧值、来源与时点。

## 不许做什么

- **不许用 ingest 车道塞 agent 的推测或补录**：ingest 的语义是「来源系统即权威，写入即事实」，不走确认门。把推测放进去，等于借导入车道的权威性洗白一个未经确认的判断。
- **不许跳过 `decide_event` 直接把拟稿事件当事实用于投影或过账**：`draft` 车道初始状态 `pending`，提前投影会让数量面先长出余额，过账也会记一笔不存在的业务。
- **不许改写已定型事件或用新事件「覆盖」旧事件**：事件只追加（`EVENT_IMMUTABLE`）；更正只走冲正事件，定型前修订走 revision 通道。
- **不许用空串、0、空数组表示载荷里确实不知道的字段**：载荷校验会拒绝空值——空串会被读成「没有」（`INVALID_ARGUMENT`）；确实未知必须显式写 `__unknown__`。
- **不许在事件载荷里携带资金池 / 代收代付 / 分账执行指令**：事件是事实载体，不是资金指令通道；这类指令一旦成为事实，下游过账会试图执行红线内的动作。

## 硬纪律

1. **agent 写路径只出 proposal 或拟稿事件**。本组是后半句的落点：agent 只能 `submit_event` 产拟稿，不能借 `ingest_event` 把判断直接写成事实；确认门由 `decide_event` 的人工岗位把住。
2. **未知必须显式 `__unknown__`**。载荷 `required` 字段缺失 → `INVALID_ARGUMENT`；空值 → `INVALID_ARGUMENT` 并点名空字段。`__unknown__` 是唯一能区分「不知道」与「没有」的表示，空串会让下游把「未查」当「没有」。
3. **余额不可直写**。事件是余额的上游：数量面余额由已接受事件投影重算；想改余额就补正确事件，不许投影后改物化值。
4. **过账须已审批规则，缺规则挂待补**。事件 `accepted` 只说明事实成立，不说明科目；过账由已审批规则派生，缺规则进 posting 待补清单，余额不变。
5. **资金类科目禁入（二清红线）**。事件可携带金额与币种字段，因此同样受红线约束：资金池、代收代付、分账执行不通过事件层进入内核；资金走客户自选持牌通道。

## 哪类岗位调哪几件

| 岗位 | 可调工具 | 说明 |
|---|---|---|
| inspector（只读巡检） | `query_events` | 看事件流与完整性声明、pending 积压；不写事实、不裁决。 |
| analyst（定性 / 拟稿） | `ingest_event`、`submit_event`、`query_events` | 外部事实导入与 agent 拟稿；不持确认门。 |
| decider（持确认门） | `decide_event`、`query_events` | 唯一能把 pending 变成 fact 的岗位；拒也要留状态。 |

`decide_event` 只出现在拍板岗：它是 「写事实」的唯一人工入口，和账本确认门同级。

## 多板块生产者（营销接入）

事件层从「ERP 单生产者」扩成**多板块、单表单注册表**（`producer` 维度：`erp` / `marketing`）。agent 要记住四件事：

1. **类型名带板块前缀**。营销类型的名字一律以 `marketing.` 开头（已登记 `marketing.content.published`、`marketing.desk.conversation_closed`、`marketing.channel.order_paid`）。类型名与 `producer` 登记不自洽会直接判红（`EVENT_PRODUCER_MISMATCH`）：单表里类型名是唯一的人工识别线索。
2. **producer 由类型注册表决定，不由调用方声明**。写事件没有「我是哪个板块」这个参数；返回值里的 `producer` 是类型登记时的归属。理由：调用方声明等于自己给自己开板块——营销自称 ERP 就能绕开 ERP 侧巡检，反向也能把账务事实标成营销事实。
3. **营销只产三类事实**：内容发布、客服会话关闭、渠道订单付款。每一类都对应仓内已存在的载体（注册表 `evidence_ref` 指出出处）；指不到载体的类型不许登记。
4. **四个名字是黑名单**：投放花费、线索漏斗、活动、转化（含 `*_spend`、`lead_*`、`*_conversion` 等变体）登记即拒（`EVENT_FABRICATED_FACT`）；营销类型缺载体出处同样拒。先补载体（表或连接器只读面）再登记类型，不许先登记类型再找人填数。

`query_events` 的 `producer` 参数是可选的：不传 = 同租户跨板块可见（不设板块墙）；传 `erp` / `marketing` 则只看一个板块。营销类型全部 `requires_posting=false`，因此不会进过账待补清单，也不会自动并入任何账本。

