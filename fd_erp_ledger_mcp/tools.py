"""8 个 MCP 工具（erp-ledger-kernel 工具面 / 任务 2.1-2.4）。

写路径只出 proposal，无任何直写落账工具；确认门是唯一落账入口。
全部工具经 safe_tool 统一错误语义 + rows_returned 计量戳。
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .annotations import annotations_for
from .config import Settings
from .db import connection
from .errors import ErpError
from . import ledger as _ledger
from .proposals import (confirm_proposal as _confirm, list_proposals as _list,
                        reject_proposal as _reject, submit_proposal as _submit)
from .recon.engine import enqueue as _enqueue, get_report as _get_report
from .result import paginate
from .tenancy import resolve_tenant
from .tooling import safe_tool


def register(mcp: FastMCP, settings: Settings) -> None:

    # ---- 1. submit_proposal：只创建 pending 提案，绝不改变账本 ----
    @mcp.tool(annotations=annotations_for("submit_proposal"))
    @safe_tool
    def submit_proposal(
        stable_id: str,
        lines: list[dict[str, Any]],
        source_ref: str | None = None,
        kind: str = "journal",
        reversal_of: str | None = None,
        lane_fleet: str | None = None,
        lane_post: str | None = None,
        lane_title: str | None = None,
        lane_target: str | None = None,
        tenant_id: str | None = None,
        submitted_by: str = "agent",
    ) -> dict[str, Any]:
        """提交拟落账事件（proposal），不直接改变账本状态；同 stable_id 重复提交返回首次结果。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _submit(conn, settings=settings, tenant_id=tenant,
                           stable_id=stable_id, lines=lines, source_ref=source_ref,
                           kind=kind, reversal_of=reversal_of,
                           lane_fleet=lane_fleet, lane_post=lane_post,
                           lane_title=lane_title, lane_target=lane_target,
                           submitted_by=submitted_by)

    # ---- 2. list_proposals ----
    @mcp.tool(annotations=annotations_for("list_proposals"))
    @safe_tool
    def list_proposals(
        status: str | None = None,
        tenant_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """分页查询本租户的 proposal 列表及状态（待确认/已确认/已拒绝）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            rows = _list(conn, tenant, status)
            return paginate(rows, offset=offset, limit=limit)

    # ---- 3. confirm_proposal：确认门（唯一落账路径）----
    @mcp.tool(annotations=annotations_for("confirm_proposal"))
    @safe_tool
    def confirm_proposal(
        proposal_id: int | None = None,
        stable_id: str | None = None,
        tenant_id: str | None = None,
        confirmed_by: str = "human",
    ) -> dict[str, Any]:
        """确认门——确认后按复式分录落账；借贷必平由数据库约束强制，不平则整笔拒绝。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _confirm(conn, settings=settings, tenant_id=tenant,
                            proposal_id=proposal_id, stable_id=stable_id,
                            confirmed_by=confirmed_by)

    # ---- 4. reject_proposal ----
    @mcp.tool(annotations=annotations_for("reject_proposal"))
    @safe_tool
    def reject_proposal(
        proposal_id: int | None = None,
        stable_id: str | None = None,
        reason: str | None = None,
        tenant_id: str | None = None,
        rejected_by: str = "human",
    ) -> dict[str, Any]:
        """拒绝 proposal，留审计痕迹且不落账。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _reject(conn, tenant_id=tenant, proposal_id=proposal_id,
                           stable_id=stable_id, reason=reason, rejected_by=rejected_by)

    # ---- 5. query_balance ----
    @mcp.tool(annotations=annotations_for("query_balance"))
    @safe_tool
    def query_balance(
        account_code: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """按科目/币种查询余额；余额由分录确定性重算，无直写余额字段。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            data = _ledger.compute_balance(conn, tenant, account_code, settings=settings)
            return {"data": data, "total": len(data), "offset": 0,
                    "limit": len(data), "truncated": False}

    # ---- 6. query_entries ----
    @mcp.tool(annotations=annotations_for("query_entries"))
    @safe_tool
    def query_entries(
        account_code: str | None = None,
        txn_id: str | None = None,
        tenant_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """分页查询已落账分录（含原币/折算、出处、来源引用、时间戳、冲正关联）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            rows = _ledger.query_entries(conn, tenant, account_code=account_code, txn_id=txn_id)
            return paginate(rows, offset=offset, limit=limit)

    # ---- 7. run_reconciliation：长任务，立即返回 task_id ----
    @mcp.tool(annotations=annotations_for("run_reconciliation"))
    @safe_tool
    def run_reconciliation(
        left_source: dict[str, Any],
        right_source: dict[str, Any],
        retry_task_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """运行两源流水对账（只读滩头），立即返回 task_id；对外部系统零写入。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
        return _enqueue(settings, tenant, left_source, right_source, retry_task_id)

    # ---- 8. get_recon_report ----
    @mcp.tool(annotations=annotations_for("get_recon_report"))
    @safe_tool
    def get_recon_report(
        task_id: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """按 task_id 查询对账任务状态、进度与差异清单（四类差异，双侧原始引用）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
        return _get_report(settings, tenant, task_id)
