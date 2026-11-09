"""19 个 MCP 工具（erp-ledger-kernel 工具面 / 任务 2.1-2.4 + 计费 1.x + 信用 4.2）。

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
from . import billing as _billing
from . import credit as _credit
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

    # ---- 9. submit_billing_rule：登记计费规则新版本（draft → pending_approval）----
    @mcp.tool(annotations=annotations_for("submit_billing_rule"))
    @safe_tool
    def submit_billing_rule(
        config: dict[str, Any],
        source_ref: str | None = None,
        tenant_id: str | None = None,
        submitted_by: str = "ops",
    ) -> dict[str, Any]:
        """提交计费规则新版本（收费项/计价单位/阶梯/封顶/最低消费/生效期）。

        config.rate_source 必填（无出处费率拒绝）；同 rule_key 版本递增；
        提交后为 pending_approval，须经 approve_billing_rule 人工确认才上线。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _billing.submit_billing_rule(conn, settings=settings, tenant_id=tenant,
                                                config=config, source_ref=source_ref,
                                                submitted_by=submitted_by)

    # ---- 10. approve_billing_rule：规则确认门（pending_approval → active）----
    @mcp.tool(annotations=annotations_for("approve_billing_rule"))
    @safe_tool
    def approve_billing_rule(
        rule_id: int,
        version: int,
        approved_by: str = "human",
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """人工确认计费规则上线（不接受程序自审自批）。

        同收费维度冲突未处置（区间相交无显式优先级 / 生效期空洞）→ 拒绝上线。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _billing.approve_billing_rule(conn, settings=settings, tenant_id=tenant,
                                                 rule_id=rule_id, version=version,
                                                 approved_by=approved_by)

    # ---- 11. list_billing_rules ----
    @mcp.tool(annotations=annotations_for("list_billing_rules"))
    @safe_tool
    def list_billing_rules(
        status: str | None = None,
        tenant_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """分页查询计费规则（draft/pending_approval/active/retired，含版本与 config）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            rows = _billing.list_billing_rules(conn, tenant, status)
            return paginate(rows, offset=offset, limit=limit)

    # ---- 12. bill_event：事件计费（幂等，写 fee_details + 机器车道 proposal）----
    @mcp.tool(annotations=annotations_for("bill_event"))
    @safe_tool
    def bill_event(
        event: dict[str, Any],
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """按 active 规则对事件计费（event 须含 stable_id/kind/occurred_at/measures/refs）。

        同 stable_id 重推返回首次结果；费用行锁定规则版本+基础数据快照可重算；
        分录走 billing-engine 机器车道 proposal（1300/4000/费用类白名单），
        落账仍须经 confirm_proposal 确认门。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _billing.bill_event(conn, settings=settings, tenant_id=tenant, event=event)

    # ---- 13. list_fees ----
    @mcp.tool(annotations=annotations_for("list_fees"))
    @safe_tool
    def list_fees(
        tenant_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """分页查询费用明细（append-only；红冲行为 status='reversed' + reversal_of）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            rows = _billing.list_fees(conn, tenant)
            return paginate(rows, offset=offset, limit=limit)

    # ---- 14. get_fee_detail：四层溯源 ----
    @mcp.tool(annotations=annotations_for("get_fee_detail"))
    @safe_tool
    def get_fee_detail(
        fee_id: int,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """费用详情 + 四层溯源链（费用项→业务单据→作业事件→计费规则版本）+ 快照重算。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _billing.get_fee_detail(conn, settings=settings, tenant_id=tenant,
                                           fee_id=fee_id)

    # ---- 15. reverse_fee：红冲（原行原样，反向行关联）----
    @mcp.tool(annotations=annotations_for("reverse_fee"))
    @safe_tool
    def reverse_fee(
        fee_id: int,
        reason: str,
        tenant_id: str | None = None,
        reversed_by: str = "human",
    ) -> dict[str, Any]:
        """红冲费用：生成反向明细行（reversal_of 关联，原行保持原样，append-only）。

        原费用分录已落账时同时组冲正 proposal（走既有确认车道）。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _billing.reverse_fee(conn, settings=settings, tenant_id=tenant,
                                        fee_id=fee_id, reason=reason, reversed_by=reversed_by)

    # ---- 16. simulate_billing：试算（零写入）----
    @mcp.tool(annotations=annotations_for("simulate_billing"))
    @safe_tool
    def simulate_billing(
        config: dict[str, Any],
        events: list[dict[str, Any]],
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """按指定历史事件集试算新旧规则费用差异（零写入；费率出处纪律同正式规则）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _billing.simulate_billing(conn, settings=settings, tenant_id=tenant,
                                             config=config, events=events)

    # ---- 17. reserve_credit：占用信用额度（备忘，不写复式分录）----
    @mcp.tool(annotations=annotations_for("reserve_credit"))
    @safe_tool
    def reserve_credit(
        customer_id: str,
        stable_id: str,
        amount: float,
        expires_at: str,
        currency: str = "CNY",
        source_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """占用客户信用额度；额度不足拒绝（CREDIT_EXCEEDED 含额度明细）。

        同 stable_id 幂等返回首次结果；占用为备忘性质，不写复式分录。
        `expires_at`（ISO 时间）必填：占用没有失效时点就无法被识别为孤儿。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _credit.reserve_credit(conn, settings=settings, tenant_id=tenant,
                                          customer_id=customer_id, stable_id=stable_id,
                                          amount=amount, currency=currency,
                                          source_ref=source_ref, expires_at=expires_at)

    # ---- 18. release_credit ----
    @mcp.tool(annotations=annotations_for("release_credit"))
    @safe_tool
    def release_credit(
        customer_id: str,
        stable_id: str,
        released_by: str = "system",
        reason: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """释放信用占用（reserved → released，幂等）；可用额度随之恢复。

        `released_by` / `reason` 落库，用于回答"谁释放的、凭什么"。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _credit.release_credit(conn, settings=settings, tenant_id=tenant,
                                          customer_id=customer_id, stable_id=stable_id,
                                          released_by=released_by, reason=reason)

    # ---- 19. query_credit ----
    @mcp.tool(annotations=annotations_for("query_credit"))
    @safe_tool
    def query_credit(
        customer_id: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """查询客户额度位：limit / ar_balance（1300 分录重算）/ reserved / available。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _credit.credit_position(conn, settings=settings, tenant_id=tenant,
                                           customer_id=customer_id)

    # ---- 20. list_credit_reservations（只读；孤儿报表的读原语）----
    @mcp.tool(annotations=annotations_for("list_credit_reservations"))
    @safe_tool
    def list_credit_reservations(
        customer_id: str | None = None,
        include_expired: bool = True,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """列出信用占用明细（只读，含派生状态 reserved/expired/released）。

        内核不知道订单，因此本工具只给"占用是什么样"；"是否孤儿"由能同时看到
        订单的一侧（模板层）判定。本工具不释放任何占用、不修改任何状态。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            rows = _credit.list_reservations(
                conn, tenant_id=tenant, customer_id=customer_id,
                include_expired=include_expired)
            # 必须走 paginate：`stamp()` 只认 `result["data"]`，自己写 rows_returned 会被
            # 覆盖成 0 —— 而 rows_returned 是"按行计费唯一依据"（result.py:4）。
            # 2026-11-09 生产实测抓到的：本工具返回 2 行却报 rows_returned=0。
            return paginate(rows, offset=0, limit=0)
