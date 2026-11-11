# fd-erp-ledger-mcp

ERP 账本内核 MCP：行业无关的最小复式事件账 + agent「只写 proposal → 确认门落账」
车道 + 只读对账引擎 + 计费规则引擎 + 信用额度。OpenSpec change `add-erp-domain`
的实施产物，计费/信用归 `add-supply-chain-vertical`（任务 1.x / 4.2）；
计费补强（多币种折算 / 收费项覆盖 / 日终快照）与运维加固（指标 / 结构化日志 /
对账续跑）归 `optimize-erp-solution`（5.1-5.6 / 6.1 / 6.5）。

## 形态与边界（如实口径）

- **组合式**：ERP 能力 = 集群 + 供给包组合；本服务只是行业无关的账本内核，
  不是单体 ERP 应用，不含表单 UI。行业模板（连接器 / 舰队 / 科目映射）归
  `add-erp-vertical-templates`。
- **执行面**：发货/支付/补货/开票等只经采纳的外部 MCP（BYOK，凭证零落库零日志）。
- **资金合规红线**：资金归集/分账走客户自选持牌通道；内核只做对账与核对指令层，
  不提供资金池、代收代付或分账执行。
- **开票/税务**：不自研、当前挂账未提供；账内可记录发票类事件并参与对账，
  票据本体由客户既有合规渠道处理。
- **租户模型**：本内核采用「逻辑隔离（租户列 + 查询闸）」起步（见 design.md）。
  `wire-cell-tenancy` 的「进程+数据根物理隔离、引擎不感知 tenant」是柏讯 DAAS
  引擎的约束，不是本新服务的约束；物理隔离诉求走 appliance 私有化档位。

## 命名（`fd-package-identity`）

| 概念 | 值 |
|---|---|
| 目录 / 发行名 / 控制台脚本（dash） | `fd-erp-ledger-mcp` |
| import 包（underscore） | `fd_erp_ledger_mcp` |

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
export ERP_LEDGER_DATABASE_URL="postgresql://user@host/db"
export ERP_TENANT_ID="acme"

fd-erp-ledger-mcp migrate          # 迁移阶段：一次性、可重放、幂等
fd-erp-ledger-mcp check-schema     # 启动前 schema 版本闸
fd-erp-ledger-mcp serve --host 0.0.0.0 --port 8000
```

零配置启动：`create_server()` 不连库；缺数据源时调用点如实报
`UPSTREAM_UNAVAILABLE`。

## 32 个工具

| 工具 | 车道 | 说明 |
|---|---|---|
| `submit_proposal` | 写(proposal) | 提交拟落账事件，同 `stable_id` 幂等返回首次结果 |
| `list_proposals` | 读 | 分页查询 proposal 状态 |
| `confirm_proposal` | 写(确认门) | 拟稿车道的唯一落账入口（导入车道的事实不需确认）；借贷必平由 DB 约束强制 |
| `reject_proposal` | 写 | 拒绝留痕、不落账 |
| `query_balance` | 读 | 余额由分录确定性重算，无直写余额字段 |
| `query_entries` | 读 | 分页查询已落账分录（含出处/来源/冲正关联） |
| `run_reconciliation` | 长任务 | 两源流水对账，立即返回 `task_id`，零外部写入 |
| `get_recon_report` | 读 | 查询任务状态/进度/四类差异清单 |
| `submit_billing_rule` | 写 | 登记计费规则新版本（无出处费率拒绝；同 rule_key 版本递增） |
| `approve_billing_rule` | 写(确认门) | 规则人工确认上线；同维度冲突未处置拒绝（不接受程序自审自批） |
| `list_billing_rules` | 读 | 分页查询计费规则（版本/状态机/config） |
| `bill_event` | 写 | 按 active 规则事件计费；同 stable_id 幂等；落账走机器车道 proposal |
| `list_fees` | 读 | 分页查询费用明细（含红冲反向行） |
| `get_fee_detail` | 读 | 四层溯源链（费用项→业务单据→作业事件→规则版本）+ 快照重算 |
| `reverse_fee` | 写 | 红冲：新增反向明细行，原行原样保留（append-only） |
| `simulate_billing` | 读 | 按历史事件集试算新旧规则费用差异（零写入） |
| `bill_period_snapshot` | 写 | 日终快照计费：按期间触发、幂等重跑、批量 proposal；覆盖缺失整批拒绝 |
| `reserve_credit` | 写 | 占用信用额度（备忘性质，不写复式分录）；不足拒绝带明细 |
| `release_credit` | 写 | 释放占用（reserved→released，幂等） |
| `query_credit` | 读 | 额度位：limit / ar_balance / reserved / available |
| `list_credit_reservations` | 读 | 信用占用明细（派生状态）；孤儿报表的读原语，不修改任何状态 |

写路径只出 proposal，无任何直写落账工具（计费/红冲产生的分录同样走 proposal
确认门，科目限机器车道白名单 1300/4000/5100/5200，禁 1000/2100/3000/3100）。

## 计费规则引擎（`add-supply-chain-vertical` 任务 1.x）

- **规则版本化 + 审批确认门**：同 rule_key 版本递增；submit（draft→pending_approval）
  → approve（→active，须人工确认者标识）→ 旧 active 版本随新版本退役。
- **上线拦截**：同收费维度（fee_item）的 active 规则，区间相交且无显式优先级
  （双方都有且不等才算处置）、或生效期存在空洞 → 审批拒绝（RULE_CONFLICT），
  未处置 MUST NOT active。
- **无出处费率拒绝**：`config.rate_source` 缺失/空白即拒绝（RATE_UNSOURCED，
  仿 FX_UNSOURCED 风格）。
- **计费幂等 + 快照**：同 `stable_id` 重推返回首次结果（UNIQUE 兜底）；每行费用
  锁定 rule_id + rule_version + 基础数据快照，可离线确定性重算
  （`get_fee_detail.snapshot_replay`）。
- **红冲**：`reverse_fee` 生成反向明细行（status='reversed' + reversal_of），
  原行原样保留；fee_details 与 journal_entries 同等 append-only（触发器 + REVOKE）。
- **日终快照计费**：MCP 工具 `bill_period_snapshot(period, snapshots[])` 对外暴露，
  存储费 = 日终库存快照 × 费率，按期间幂等（见「日终快照计费：触发与 CronJob 契约」）。
- **试算**：`simulate_billing` 按历史事件集对比新旧规则费用差异，零写入。

## 多币种计费折算（迁移 0005）

- **费用明细自带四要素**：`fee_details` 落 `original_amount`（原币）/ `base_amount`
  （本位币）/ `fx_rate` / `fx_as_of` / `fx_source`，费用行可独立审计，不必翻账本才能
  解释金额。本位币费用的 `fx_*` 为 NULL；DB CHECK `erp_fee_fx_provenance` 要求
  「汇率三要素要么齐、要么全空」，`erp_fee_amount_pair` 要求原币/本位币成对非负。
  0005 之前的存量行不补造折算口径（补造等于凭空发明汇率）。
- **有出处才折算**：出处取规则 `config.fx_source`，缺省回落
  `Settings.fx_source`（`ERP_FX_SOURCE`，默认 `ecb-exr`）。
  - 两边都没有出处 → `FX_UNSOURCED`（detail.reason=`no_source`），不猜默认汇率；
  - 该币种在该时点没有登记事实 → `FX_UNSOURCED`（detail.reason=`no_registered_rate`），
    点名币种与时点；
  - 账本层第二道闸：分录声明的 rate/as_of/source 必须在 `fx_facts` 有同值事实，
    与登记不符同样 `FX_UNSOURCED`。
- **拒绝先于写入**：全部费用先解析折算、再写 `fee_details` / proposal，缺汇率时
  费用零行、proposal 零条、分录零条（不留半批），也绝不静默跳过外币费用。
- **分录按汇率事实分组**：`(currency, fx_rate, fx_as_of, fx_source)` 相同才汇总成行；
  借贷两侧同额 → 折算后的 `amount_base` 相等，借贷必平仍由 DB 在 COMMIT 判定。

## 收费项登记与覆盖缺失门禁（迁移 0005）

- **登记表 `billing_fee_items`**：`(tenant_id, fee_item, event_kind, status)`，
  `in_use` = 在用（参与差集）、`retired` = 停用（保留登记痕、退出差集）。
  登记/停用是运维目录动作，走 `fd-erp-ledger-mcp register-fee-item`
  （MCP 工具面口径固定，不为登记扩工具）。
- **差集口径**：`登记在用集合 − active 规则集合` 非空即阻断，三个拦截点共用：
  `rule_approval`（规则审批上线）、`event_billing`（事件计费）、
  `period_billing`（日终快照计费，整期间判定、不生成部分批次）。
  错误码 `FEE_COVERAGE_MISSING`，`detail.fee_items` 点名缺失收费项、`detail.stage`
  标明拦截点。
- **审批管道豁免**：已有 `pending_approval` 规则的收费项不计入缺口——它本身就是待
  上线候选，否则「两个收费项依次上线」会永久死锁。「停旧未上新」（无任何 pending
  候选）仍会被点名拒绝。
- **迁移过渡**：租户一条登记行都没有时视为「尚未启用登记」，不启用覆盖闸，存量
  行为不变；一旦有任意登记行（含 retired），差集对全部拦截点立即生效。

## 日终快照计费：触发与 CronJob 契约

- **两个等价入口**：MCP 工具 `bill_period_snapshot(period, snapshots[])`（外部排程/
  Agent 可调）与 CLI `fd-erp-ledger-mcp bill-period --period ... --snapshot-file ...`
  （CronJob 打点）。库内函数不是唯一入口。
- **幂等键 = period**：`event_stable_id = bill-period:<period>`，明细
  `bill-period:<period>:<rule_key>`；同期间重跑返回首次结果（`duplicate=true`），
  不产生重复费用，所以 Job 可安全重试。失败时 CLI 以非零退出（不是「跑完了但没做事」）。
- **批量 proposal 进确认门**：费用落 `fee_details` + 一条 billing-engine 机器车道
  proposal；未确认前不落账，落账仍只能走 `confirm_proposal`。
- **覆盖缺失整批拒绝**：登记在用项无 active 规则、或请求快照含未登记收费项 →
  整批拒绝并点名，不生成部分批次。
- **CronJob 契约**（清单侧，挂账）：CST 00:10 触发结算前一日，容器内须
  `TZ=Asia/Shanghai`；镜像用与 Deployment 相同的不可变 `sha-*` tag，DSN / 汇率源
  等经同一个 `Secret(fd-erp-ledger-mcp-env)` 注入；快照文件由 Job 挂载（ConfigMap /
  对象存储下载），每期一份、每项收费项只能报一份日终快照。
  当前 `deploy/k8s/erp-ledger-mcp.yaml` **未包含 CronJob 清单**（发版步骤，尚未落地）。

## 信用额度（任务 4.2）

- **口径（design 已裁定）**：`available = limit − 1300 应收余额（分录确定性重算）
  − active reservations 之和`；应收科目口径为通用科目 1300 在途结算款
  （租户映射科目留待模板层接入）。
- **备忘性质**：占用/释放不写任何复式分录；`credit_reservations` 仅做状态推进
  （reserved→released）。
- **幂等**：同 `stable_id` 重推返回首次结果；额度不足拒绝（CREDIT_EXCEEDED，
  detail 含上限/应收/已占用/请求/可用）。
- **额度登记**：MCP 工具面固定为 reserve/release/query 三件；额度上限的登记/
  调增走运维命令 `fd-erp-ledger-mcp set-credit-limit`（带 source_ref 出处）。

## 数据库层强制的不变量（`migrations/0001_baseline.sql` / `0003_billing.sql` / `0005_billing_fx_provenance.sql`）

- **借贷必平**：`DEFERRABLE INITIALLY DEFERRED` 约束触发器在 COMMIT 时按 txn
  校验 `SUM(debit)=SUM(credit)`；应用层无法绕过。
- **append-only**：`BEFORE UPDATE/DELETE/TRUNCATE` 触发器拒绝改/删已落账分录与
  已生成费用明细；`REVOKE` 双保险。更正只走冲正（反向分录/反向费用行 +
  `reversal_of` 关联），原行原样保留。
- **幂等**：`UNIQUE(tenant_id, stable_id, line_no)`（分录）与
  `UNIQUE(tenant_id, stable_id)`（费用/信用占用）；同 stable_id 重推不重复入账。
- **无出处拒绝**：汇率 `erp_fx_unsourced` CHECK + `fx_facts` 出处登记；计费费率
  `config.rate_source` 必填，双闸同源（无出处不入账）。
- **来源与时间戳**：每行分录/费用带 `stable_id` / `source_ref` / 时间戳 /
  `provenance`（费用含四层溯源链）。

`db-schema-lifecycle`：`migrate` 是唯一发 DDL 的阶段，一次性、可重放（已登记
版本跳过）、不做手工改生产库；`check-schema` 启动比对版本，落后即 fail-fast。

### 迁移 0005 / 0006

| 版本 | 内容 | 回退口径 |
|---|---|---|
| `0005_billing_fx_provenance` | `fee_details` 增 `original_amount` / `base_amount` / `fx_rate` / `fx_as_of` / `fx_source` + 两个 CHECK；新增 `billing_fee_items` 收费项登记表 | 只增列/增表，不删不改既有数据；存量行折算列留 NULL |
| `0006_recon_task_request` | `recon_reports` 增 `request` JSONB（左右源规格 + retry_task_id） | 存量行 `request` 为 NULL，启动时如实标 interrupted |

代码要求的 schema 版本是 `REQUIRED_SCHEMA_VERSION = "0006_recon_task_request"`；
发版顺序仍是 `migrate` → `check-schema` → `serve`，运行时绝不发 DDL。迁移可重放：
已登记版本直接跳过，连 `applied_at` 都不动。

## 对账引擎（只读滩头）

- 源适配器：`inline` / `csv` / `api`（通用三型；平台连接器归模板 change），
  只读、无 write/push 路径。
- 四类差异：`missing_in_right` / `extra_in_right` / `amount_mismatch` /
  `status_mismatch`，每条含双侧原始引用与原始记录快照。
- 长任务（`mcp-long-running-operations`）：立即返回 task_id，状态
  queued/running/done/failed，进度单调，逐条成败落库，仅失败条目可
  `retry_task_id` 重试。

### 重启续跑（迁移 0006 / 6.5）

- **请求快照入库存**：`recon_reports.request` 存左右源规格 + `retry_task_id`，
  这是重启后「续跑/重排」的唯一依据（旧 schema 只存状态，重启只能标 interrupted）。
- **启动处置 `resume_orphans`**：`serve` 在 schema 版本闸之后调用，扫上次遗留的
  queued/running 任务：
  - 请求快照齐全 → 打回 `queued` / `stage='requeued'`、重起 worker，真的跑完续跑；
    `processed` 单调 + 逐条 upsert 幂等，等于续跑而不是重算，`error` 列保留重排留痕；
  - 无请求快照的存量行 → 显式标 `interrupted` 并写原因（没快照就没法续跑，不伪造）。
- **可见性**：处置结果进 `/health` 的 `recon_startup`（state/at/found/requeued/
  interrupted，只报 task_id 与动作，不含租户），并逐条写结构化日志
  `recon_restart_disposition` + 汇总 `recon_startup_disposition`，不静默消失。
- **`mark_interrupted` 保留**为运维「不要再续跑，全部中断」的显式动作；重启路径
  不再调它（那正是「静默留 interrupted」的根因）。

## 运行指标与结构化日志（`/metrics`，6.1）

- **`GET /metrics`**：`prometheus_client` 文本暴露格式，独立 registry、纯内存、
  零数据库往返；`create_server()` 零配置也能抓。暴露面纪律：只绑集群内地址、
  不挂公网 Ingress（清单侧约束）。
- **四类运行信号**（`fd_erp_ledger_mcp/observability.py` 的注册名）：
  - proposal 车道：`erp_proposals_submitted_total{kind}` /
    `erp_proposals_confirmed_total` / `erp_proposals_rejected_total`；
  - 计费：`erp_fees_billed_total{path,currency}`（path=event/period）、
    `erp_billing_failures_total{reason}`（reason=封闭错误码：`FX_UNSOURCED` /
    `FEE_COVERAGE_MISSING` / …，按原因分别成序列、不合并为单一错误计数）；
  - 对账：`erp_recon_tasks_total{status}`、`erp_recon_diffs_total{diff_type}`、
    重启处置 `erp_recon_restart_disposition_total{action}`；
  - 工具面：`erp_tool_requests_total{tool,outcome}`、
    `erp_tool_errors_total{tool,code}`、`erp_tool_latency_seconds{tool}`（histogram）。
- **结构化日志**：JSON 行写 stdout（logger `erp.kernel`），事件名如
  `proposal_submitted` / `proposal_confirmed` / `proposal_rejected` /
  `fees_billed` / `billing_failed` / `tool_call` / `recon_restart_disposition`。
- **脱敏（BYOK 零落库红线）**：租户只以 12 位 sha256 前缀 `tenant_hash` 出现，
  不落 `tenant_id` 明文；`redact()` 按「键名命中」（password/token/dsn/
  database_url/...）与「值形状像连接串」双闸打码。指标不带任何租户标签。
- **`GET /health`**：承载探针（不碰库），`recon_startup` 报告启动时遗留对账任务的
  处置结果。

## 计量 / 错误 / 结果契约

- 每次成功响应带 `rows_returned` 戳（`wire-facade-metering`）；写工具落 0，
  不以字段缺失代替 0。
- 错误码来自封闭集合（`error_codes.py`，≥4 类可区分语义），机器可读 code +
  可读描述 + 修正建议；失败绝不返回空列表伪装成功。
- 统一体积上限 + 分页续读（`result.py`），超限返回截断标记 + 原始规模。

## 测试

```bash
ERP_LEDGER_DATABASE_URL="postgresql://..." .venv/bin/pytest -q
```

测试需真实 Postgres（DB 约束层不可 mock）；三层兜底：约束层、车道层、幂等层，
外加 proposal→确认→落账→余额→对账→冲正全链 e2e，以及计费（规则审批/冲突拦截/
幂等/快照重算/红冲/溯源/试算）与信用（占用释放/额度拦截/账本衍生口径）套件。
计费补强与运维加固另有专项验收：

- `tests/test_billing_e2e.py`：一条真库用例串起「事件计费 → 日终快照计费 → 确认门
  → 落账 → 余额 → 快照重算 → 红冲」全链；另附排程工具入口产出待确认批次，以及
  「断点即整体失败、不留部分落账」反例；
- `tests/test_billing_fx.py` / `tests/test_billing_coverage.py`：多币种折算四要素与
  无出处拒绝、收费项覆盖缺失三个拦截点；
- `tests/test_observability.py`：`/metrics` 可抓取、四类信号计数、失败原因可区分、
  日志与指标脱敏；
- `tests/test_recon_resume.py`：重启续跑/重排、不可恢复遗留任务标 interrupted、
  `/health.recon_startup` 可见。

工具面 selfcheck（离线 stdlib-only 双向断言）：

```bash
python3 scripts/selfcheck_surface.py
```
