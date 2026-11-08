"""ERP 账本内核 FastMCP 服务器工厂。

零配置可启动（create_server 不连库）：与 fd-legal-search-mcp 同构，
缺数据源时调用点如实报 UPSTREAM_UNAVAILABLE，而非 import 期崩溃。
"""
from __future__ import annotations

from fastmcp import FastMCP

from . import tools
from .config import Settings

SERVER_NAME = "fd-erp-ledger-mcp"
INSTRUCTIONS = (
    "ERP 账本内核：行业无关的最小复式事件账 + 确认车道 + 只读对账滩头。"
    "agent 只能提交 proposal；确认门是唯一落账路径；借贷必平由数据库约束强制；"
    "已落账分录只进不改（更正走冲正）；跨租户查询拒绝并审计留痕。"
    "对账为只读滩头，对外部系统零写入。资金归集/分账、开票/税务不在本内核能力范围。"
)


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    mcp: FastMCP = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    tools.register(mcp, settings)
    _register_health(mcp, settings)
    return mcp


def _register_health(mcp: FastMCP, settings: Settings) -> None:
    """承载探针：`GET /health`（harden-erp-operations 4.1）。

    为什么需要：此前的 Deployment 只有 `tcpSocket` 就绪探针、没有 liveness，而进程
    僵死时 socket 仍在 → 探针给假绿灯、永不重启（2026-11-08 实测 `/health` `/healthz`
    `/` 全 404，压根没有 HTTP 健康面）。

    纪律：**不碰库、不碰业务数据、不回显租户与 DSN** —— 探针会被集群反复打，任何
    数据库往返都会把健康检查变成压力源；任何身份回显都是泄漏面。
    """

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request):  # noqa: ANN001 - fastmcp 传 Request
        from starlette.responses import JSONResponse

        return JSONResponse(
            {
                "ok": True,
                "service": SERVER_NAME,
                # 只报"承载形态"，不报具体地址（地址含集群内部名）
                "kernel_transport": "mcp",
            }
        )
