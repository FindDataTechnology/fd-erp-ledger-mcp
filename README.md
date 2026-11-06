# fd-erp-ledger-mcp

ERP 账本内核 MCP：行业无关的最小复式事件账 + agent「只写 proposal → 确认门落账」
车道 + 只读对账引擎。OpenSpec change `add-erp-domain` 的实施产物。

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

## 8 个工具

| 工具 | 车道 | 说明 |
|---|---|---|
| `submit_proposal` | 写(proposal) | 提交拟落账事件，同 `stable_id` 幂等返回首次结果 |
| `list_proposals` | 读 | 分页查询 proposal 状态 |
| `confirm_proposal` | 写(确认门) | 唯一落账路径；借贷必平由 DB 约束强制 |
| `reject_proposal` | 写 | 拒绝留痕、不落账 |
| `query_balance` | 读 | 余额由分录确定性重算，无直写余额字段 |
| `query_entries` | 读 | 分页查询已落账分录（含出处/来源/冲正关联） |
| `run_reconciliation` | 长任务 | 两源流水对账，立即返回 `task_id`，零外部写入 |
| `get_recon_report` | 读 | 查询任务状态/进度/四类差异清单 |

写路径只出 proposal，无任何直写落账工具。

## 数据库层强制的不变量（`migrations/0001_baseline.sql`）

- **借贷必平**：`DEFERRABLE INITIALLY DEFERRED` 约束触发器在 COMMIT 时按 txn
  校验 `SUM(debit)=SUM(credit)`；应用层无法绕过。
- **append-only**：`BEFORE UPDATE/DELETE/TRUNCATE` 触发器拒绝改/删已落账分录；
  `REVOKE` 双保险。更正只走冲正（反向分录 + `reversal_of` 关联）。
- **幂等**：`UNIQUE(tenant_id, stable_id, line_no)` 唯一索引；同 stable_id 重推不重复入账。
- **无出处汇率拒绝**：`erp_fx_unsourced` CHECK + `fx_facts` 出处登记双闸。
- **来源与时间戳**：每行分录带 `stable_id` / `source_ref` / `posted_at` / `provenance`。

`db-schema-lifecycle`：`migrate` 是唯一发 DDL 的阶段，一次性、可重放（已登记
版本跳过）、不做手工改生产库；`check-schema` 启动比对版本，落后即 fail-fast。

## 对账引擎（只读滩头）

- 源适配器：`inline` / `csv` / `api`（通用三型；平台连接器归模板 change），
  只读、无 write/push 路径。
- 四类差异：`missing_in_right` / `extra_in_right` / `amount_mismatch` /
  `status_mismatch`，每条含双侧原始引用与原始记录快照。
- 长任务（`mcp-long-running-operations`）：立即返回 task_id，状态
  queued/running/done/failed，进度单调，逐条成败落库，仅失败条目可
  `retry_task_id` 重试；重启后遗留任务如实标记 `interrupted`。

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
外加 proposal→确认→落账→余额→对账→冲正全链 e2e。
