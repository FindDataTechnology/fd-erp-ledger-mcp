# fd-erp-ledger-mcp · deploy（publish-READY，未上线）

本目录是 OpenSpec change `add-erp-domain` **task 2.5** 的「工具面注册与可发现性」产物之一：
一份**可发布（publish-ready）**的部署参考清单 + 客户端连接示例。**它是工件，不是上线结果。**

## 本目录有什么

| 文件 | 作用 |
|---|---|
| `k8s/erp-ledger-mcp.yaml` | Deployment + Service + PVC 参考清单，按 `mcp-service-deploy` 标配件编写（数据 PVC、缓存目录 env、`fsGroup` 属主修复、就绪/存活探针、CPU/内存 requests+limits、非 root、不可变 `sha-*` 镜像占位、HTTP on `0.0.0.0`、鉴权/DSN/BYOK 经环境变量注入）。 |
| `../mcp.example.json` | 客户端连接示例（`mcpServers` 条目 + 本机 `http://127.0.0.1:<port>/mcp` 占位）。 |
| `../tool-surface.json` | `mcp-tool-surface/v1` 上牌清单（19 工具全 commercial + 行为属性 + 封闭错误码集合 + 输出结构）。 |
| `../scripts/selfcheck_surface.py` | 离线 stdlib-only 双向断言（manifest ↔ 运行时真值 + 暴露 ⊆ commercial）。 |

## 如实边界（务必读）

- **本清单 publish-READY，NOT yet rolled out。** 没有 live 镜像（`sha-0000000` 是 NOT-YET-BUILT 占位符，
  直接 `kubectl apply` 会 `ImagePullBackOff`）、没有 ArgoCD 同步、**没有网关路由变更**、没有 NodePort 分配。
- **未改动 `fd-infra-deploy/`。** 线上唯一事实源是 `fd-infra-deploy/all-services/prod/`（ArgoCD selfHeal）。
  本 apply 刻意不碰它；真正发版时须把清单落到那里并提交。
- **registry 快照未 live 写入、网关路由未上线、谦面市场未上架、镜像未构建、官网未 deploy。**
  `MCP-REGISTRY.md` 里本服务的两行是「台账先行」的预登记（如实标注「已上牌待发布/未上线」），
  不是注册完成或已在路由的声明。
- **零明文密钥。** 鉴权 / Postgres DSN / BYOK / 汇率源凭据全部由部署侧 out-of-band 的
  `Secret(fd-erp-ledger-mcp-env)` 经环境变量注入（BYOK 零落库零日志）。本仓、本清单、`mcp.example.json`
  均不含任何 token / 密码 / 凭据。
- 板块口径如实：`product_line: erp` 是 **board 级板块标签**，不是五条 ladder 业务线之一；
  本服务是**账本内核**（复式事件账 + 确认车道 + 只读对账滩头），不做单体 ERP / 开票自研 /
  资金执行（二清红线，资金走客户自选持牌通道）。

## 固定发版通道（均为 DEPLOY 步骤，本 apply 挂账）

需要 live 凭据与集群写权限，本 apply 不执行、仅登记为挂账：

```
代码仓 gitee push
  → Jenkins 构建
  → Harbor 推不可变 sha-* tag（禁止 :latest / 浮动标签）
  → 修改 fd-infra-deploy 中 image tag 并提交
  → ArgoCD 自动滚动 fd-prod
  → 端口台账 PORTS.md 先行登记 NodePort（冻结端口保护）
  → 网关路由 / registry 元数据（metadata.surface.commercial_tools 名单表，
     由 scripts/export_surface_commercial.py 从 tool-surface.json 单一推导）刷新
  → 谦面市场 pack 上架（task 7.1）/ appliance 三档适配（task 7.2，挂账）
```

> `scripts/export_surface_commercial.py`（根仓）当前不含本 server 的条目；把
> `fd-erp-ledger-mcp` 加入其 `SERVERS` 映射并刷新 registry 名单表属于发版动作，本 apply 未做。

## 本地能验证什么（不触网、不触集群）

```bash
# 从 finddata 根执行
python3 fd-erp-ledger-mcp/scripts/selfcheck_surface.py            # 双向断言 PASS
python3 -c "import json; d=json.load(open('fd-erp-ledger-mcp/tool-surface.json')); \
assert d['registry']['expected_tool_count']==8 and len(d['tool_annotations'])==8"
```

YAML 仅做静态阅读/语法解析，不做 `kubectl apply` / `argocd sync` / 镜像构建 / 网关注册。
