# ERP 账本内核（erp-ledger）线的授权与凭据证据

本文件是 `line-manifest.json` 被剥离的自由文本的**仓内**留存。原因：清单会随
`/.well-known/line-manifest.json` **匿名公开**，而规格（`openspec` 的
`line-manifest` 能力）规定清单字符串键只能落在一个闭集里——闭集之外的自由文本
（典型是 `note`）一律判违规：一段自由文本不是「写短点」就安全，它压根不该出现在
公开契约里。下列内容含环境变量名、内部 ClusterIP、模块与迁移文件路径，故只留在
仓库内，不进公开载荷。

对外可见的那部分是产品语义：ERP 账本内核——复式事件账 + 确认车道 + 只读对账滩头，
行业无关、多租户、多币种、append-only 存证。

## 被剥离的 note（原文逐字，按清单路径索引）

### `<root>`

本清单刻意留一条 repo-class FAIL「回退轨缺失」：ERP 今天既无线自有常命钥匙（ERP_BYOK_ 是出站凭证，byok.py:10、recon/adapters.py:169-174），也无入站凭据校验，故没有任何 enforced 轨可标 fallback。surfaces.account=false（无按客户的用量/账单面，billing 暴露的是业务域费用数据）；surfaces.admin=false（运维动作全是 CLI 子命令：migrate/check-schema/mark-adopted/seed-fx/set-credit-limit/register-fee-item，无 HTTP 管理面，无 line:admin）。两处必须留痕的漂移：① 线上镜像源 757a83e 是 21 工具，仓内 tool-surface 声明 32（32 工具的 91faa4e 与事件内核 bdb800d 都不在 757a83e 内）；② tool-surface 的「业务事件内核…账本投影」是仓 HEAD 的目标态，线上仍是 pre-event-kernel 的账本内核。另：deploy/README.md 写发布通道是 gitee，实际远端是 GitHub scs001，两处不一致属既有事实。

### `identity`

issuer 记的是该线客户身份所属的租户 IdP，不是本仓已实现的验签：本仓的**租户/OIDC 身份轨尚未实现**（无断言兑换端点，故 jit_policy=off）。**但线侧凭据校验已有一层**：2026-11-13 起 `MCP_BEARER_TOKEN` 共享 bearer 门在运行路径生效（`fd_erp_ledger_mcp/auth.py`；恒时比较、每请求重读、lifespan 放行；豁免面 `/health` `/healthz` `/metrics`，数据面 `/mcp` 受门）——即 `auth_tracks` 里 `line-native / mcp-bearer-token` 那条。该共享钥匙**不承载租户/客户身份**：跨租户拒绝与审计留痕仍由本线代码按调用身份做，每客户一把钥匙属词元轨（`ciyuan-sk`，尚未生效）。公网入口 https://mcp.finddatatech.cloud/fd-erp-ledger-mcp/mcp 的那道 401 由网关判（网关只做纯路由 + 剥调用方 Authorization，再用注册表里的凭据覆盖）；集群 mesh http://cheap6.mesh.finddata.cn:31910/mcp 直连时线内门照样验这把令牌。

### `auth_tracks[]`

线自有共享钥匙（`MCP_BEARER_TOKEN`），**已在运行路径生效**（2026-11-13 落门，change converge-line-identity-and-keys 8.5；机制与其余七条线同族）。门在 `fd_erp_ledger_mcp/auth.py`：恒时比较、每请求重读、lifespan 放行；**豁免面是三条**——`/health`（liveness 的唯一判据）、`/metrics`（ops-console 直连 ClusterIP 抓的看板数据源，写死 `10.43.146.146/metrics`）、`/healthz`（本线没有这条路由，按家族约定一并豁免）；数据面 `/mcp` 不在豁免里。翻转两步：① 注册表该 server 行 `auth_scheme=bearer` + `auth_credential`=同一把令牌，出口由 auth-server 把 `Authorization` **换成**它 → ② 线侧 Secret `fd-erp-ledger-line-token` 注入 `MCP_BEARER_TOKEN` 并滚版。**爆炸半径已核**：唯一经网关的消费者是 paas `platform` 的 `fd-erp-ledger-mcp` 条目（`credentialRef: registry`，走 `https://mcp.finddatatech.cloud/...`），由后端注入覆盖；`erp-daily-billing` 是同镜像内 CLI（`bill-period`），不过 HTTP；`ops-console` 只直连 `/health` 与 `/metrics`（都在豁免面）。**这就是 fallback 轨**：摘掉壹座后，持有该令牌的客户仍能直接消费本线。⚠️ 共享钥匙**不承载租户/客户身份**——跨租户拒绝与审计留痕仍由本线代码按调用身份做，每客户一把钥匙属词元轨（见 `ciyuan-sk` 那条的 `migration_task`）。

### `auth_tracks[]`

未接：本线**已有**入站凭据校验（`MCP_BEARER_TOKEN` 共享 bearer 门，见 `mcp-bearer-token` 那条），但**不解析**词元钥匙——`ciyuan-sk` 打进来会被当成不匹配的 bearer 而 401，不是被当作不存在。客户现在的实际路径是「平台 registry 凭据经网关代调」（第三方/平台中介）。要真接词元轨，须有词元校验面（验签/换票）+ 由它给出的客户身份，属 8.3 建立机制后的工作。

### `billing`

本仓零 sub2api/词元平面代码：计量只到「每个成功响应写 rows_returned 戳」（result.py:14-21、metering.py:1-7 明说额度闸/审计事件由 registry 承担、本模块不臆造计费动作）。按调用扣费发生在网关/registry（词元平面），不在本线代码里。billing.py 是**业务域费用**（存储费类，规则版本化 + 审批 + 红冲，migrations/0003_billing.sql:41-76），不是向本线消费者计费的证据——故不写 own。
