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
    return mcp
