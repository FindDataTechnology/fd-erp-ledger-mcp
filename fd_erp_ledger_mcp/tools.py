"""37 个 MCP 工具（账本面 2.1-2.4 + 计费 1.x + 信用 4.2 + 事件/数量/过账 establish-erp-event-kernel 5.1）。

agent 写路径只出 proposal 或拟稿事件（确认门是拟稿的唯一落账入口）；外部事实经导入车道上牌直收，不经确认门。
全部工具经 safe_tool 统一错误语义 + rows_returned 计量戳。
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .annotations import annotations_for
from .config import Settings
from .db import connection, fetch_all
from .errors import ErpError
from . import billing as _billing
from . import credit as _credit
from . import events as _events
from . import ledger as _ledger
from . import periods as _periods
from . import posting as _posting
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
        dimensions: dict[str, str] | None = None,
        submitted_by: str = "agent",
        booked_on: str | None = None,
    ) -> dict[str, Any]:
        """提交拟落账事件（proposal），不直接改变账本状态；同 stable_id 重复提交返回首次结果。

        `dimensions` 可选：键为封闭集（department/project/channel/cost_center/entity/
        customer/production_order），值为自由字符串；确认后逐行带进分录。
        不传即「未标注维度」，不补默认值。

        `booked_on` 可选：**业务记账日**（`YYYY-MM-DD`），不是受理时刻。模板层按业务
        日期归期（期初/结账/红冲/汇兑重估等都由模板算出 `day` 再传），不传则由内核按
        事件时刻推。`_submit` 一直支持它，但**工具面此前没声明** ⇒ MCP 形态下模板的
        `_kernel.submit_proposal(..., booked_on=day)` 直接 `TypeError`（14 处调用点跨
        9 个模板），而同进程形态一直正常——与 `ingest_event.provenance` 同族。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _submit(conn, settings=settings, tenant_id=tenant,
                           stable_id=stable_id, lines=lines, source_ref=source_ref,
                           kind=kind, reversal_of=reversal_of,
                           lane_fleet=lane_fleet, lane_post=lane_post,
                           lane_title=lane_title, lane_target=lane_target,
                           dimensions=dimensions, booked_on=booked_on,
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

    # ---- 3. confirm_proposal：确认门（拟稿车道的落账入口）----
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
        dimensions: dict[str, str] | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """分页查询已落账分录（含原币/折算、出处、来源引用、时间戳、冲正关联、维度）。

        `dimensions` 过滤为「与」语义：给出多个键即要求全部命中；只影响读取。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            rows = _ledger.query_entries(conn, tenant, account_code=account_code,
                                         txn_id=txn_id, dimensions=dimensions)
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

    # ---- 17. bill_period_snapshot：日终快照计费（期间触发，批量、幂等）----
    @mcp.tool(annotations=annotations_for("bill_period_snapshot"))
    @safe_tool
    def bill_period_snapshot(
        period: str,
        snapshots: list[dict[str, Any]],
        source_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """日终快照计费（存储费 = 日终库存快照 × 费率），按期间触发、幂等可重跑。

        snapshots 每项：{fee_item, as_of, measures, [refs]}；同 period 重跑返回首次
        结果（duplicate=true），不产生重复费用。覆盖缺失（登记在用项无 active 规则、
        或请求含未登记收费项）整批拒绝，不生成部分批次。费用落 fee_details + 一条
        billing-engine 机器车道 proposal，落账仍须经 confirm_proposal 确认门。
        外部排程（CronJob）用 `fd-erp-ledger-mcp bill-period` 打点，等价入口。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _billing.bill_period_snapshots(
                conn, settings=settings, tenant_id=tenant, period=period,
                snapshots=snapshots, source_ref=source_ref)

    # ---- 18. reserve_credit：占用信用额度（备忘，不写复式分录）----
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

    # ---- 19. release_credit ----
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

    # ---- 20. query_credit ----
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

    # ---- 21. list_credit_reservations（只读；孤儿报表的读原语）----
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

    # =========================================================================
    # 事件层 / 数量面 / 过账层（change: establish-erp-event-kernel 5.1）
    # 三组共用一条纪律：读取带完整性声明、写入幂等、缺规则不猜科目。
    # =========================================================================

    # ---- 22. ingest_event：外部原始事实，写入即是事实 ----
    @mcp.tool(annotations=annotations_for("ingest_event"))
    @safe_tool
    def ingest_event(
        event_type: str,
        stable_id: str,
        occurred_at: str,
        payload: dict[str, Any],
        source_ref: str | None = None,
        provenance: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """写入来自外部系统的原始事实（导入车道）。

        写入即为事实（accepted），参与数量投影与过账；同 (租户, 车道, stable_id)
        幂等，重复提交返回首次结果。事件只追加：定型后不可改写，更正须走冲正。

        occurred_at 是**事实发生时点**（不是写入时点），ISO-8601 字符串。
        载荷字段确实未知时写 "__unknown__"，不要写空串——空串会被读成"没有"。

        `provenance` 是**来源留痕**（谁/哪个外部系统报来这一条：供应商到货预告带
        `source` / `arrival_ref` / `po_ref` / `requested_by`），随事件落库供审计回溯。
        `_events.append_event` 一直支持它，但**工具面此前没声明** ⇒ MCP 形态调用
        带 provenance 会被 pydantic 判 `unexpected_keyword_argument`（2026-11-15
        生产实测：模板层到货预告在 MCP 形态 `ToolError` 直上 500，而同进程形态
        一直正常——两种传输形态分叉，正是本工具面该补的一格）。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _events.append_event(
                conn, settings=settings, tenant_id=tenant, event_type=event_type,
                stable_id=stable_id, occurred_at=occurred_at, payload=payload,
                lane="ingest", source_ref=source_ref, provenance=provenance,
                actor="ingest-lane")

    # ---- 23. submit_event：agent 拟稿，未确认不算事实 ----
    @mcp.tool(annotations=annotations_for("submit_event"))
    @safe_tool
    def submit_event(
        event_type: str,
        stable_id: str,
        occurred_at: str,
        payload: dict[str, Any],
        source_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """提交拟稿事件（拟稿车道）。落为 pending，不参与投影、不过账，直到确认。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _events.append_event(
                conn, settings=settings, tenant_id=tenant, event_type=event_type,
                stable_id=stable_id, occurred_at=occurred_at, payload=payload,
                lane="draft", source_ref=source_ref, actor="agent")

    # ---- 24. decide_event：确认门 ----
    @mcp.tool(annotations=annotations_for("decide_event"))
    @safe_tool
    def decide_event(
        stable_id: str,
        decision: str,
        note: str = "",
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """对拟稿事件作出确认或拒绝（唯一改变事件状态的人工入口）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _events.decide_event(
                conn, tenant_id=tenant, stable_id=stable_id, lane="draft",
                decision=decision, actor="human", note=note)

    # ---- 25. query_events：带完整性声明 ----
    @mcp.tool(annotations=annotations_for("query_events"))
    @safe_tool
    def query_events(
        event_type: str | None = None,
        lane: str | None = None,
        status: str | None = None,
        producer: str | None = None,
        limit: int = 100,
        after_event_id: int = 0,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """查询事件流；返回 completeness（truncated 为真时不得当成全部）。

        `producer` 可选按板块过滤（erp / marketing）；不传则同租户跨板块可见。
        返回值每条都带 producer——板块归属由类型注册表决定，不是调用方声明。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            out = _events.query_events(conn, tenant_id=tenant, event_type=event_type,
                                       lane=lane, status=status, producer=producer,
                                       limit=limit, after_event_id=after_event_id)
            return {"data": out["items"], "completeness": out["completeness"]}

    # ---- 26. query_object_balance：数量面 ----
    @mcp.tool(annotations=annotations_for("query_object_balance"))
    @safe_tool
    def query_object_balance(
        object_type: str,
        object_key: str,
        dimension: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """读数量面余额（含单位/维度/水位）+ 重算对照。

        drift 为真表示物化值与事件重算不一致——以事件为准。账本面不提供数量查询：
        金额在 ledger 组，数量在本组，两不相混。
        """
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            out = _events.query_object_balance(
                conn, tenant_id=tenant, object_type=object_type,
                object_key=object_key, dimension=dimension)
            return {"data": [out], "completeness": out["completeness"]}

    # ---- 27. project_event：事件 → 对象数量面 ----
    @mcp.tool(annotations=annotations_for("project_event"))
    @safe_tool
    def project_event(
        event_id: int,
        object_type: str,
        object_key: str,
        quantity_delta: float,
        unit: str,
        dimension: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """把已接受事件投影到数量面。同 (事件, 对象, 维度) 幂等不叠加。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            out = _events.project_object_event(
                conn, tenant_id=tenant, event_id=event_id, object_type=object_type,
                object_key=object_key, quantity_delta=quantity_delta, unit=unit,
                dimension=dimension)
            return {"data": [out], "completeness": out["completeness"]}

    # ---- 28. submit_posting_rule：规则草稿 ----
    @mcp.tool(annotations=annotations_for("submit_posting_rule"))
    @safe_tool
    def submit_posting_rule(
        event_type: str,
        lines: list[dict[str, Any]],
        source_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """提交过账规则草稿（版本化，不参与过账直到审批）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _posting.submit_posting_rule(
                conn, tenant_id=tenant, event_type=event_type,
                rule={"lines": lines}, source_ref=source_ref, actor="agent")

    # ---- 29. approve_posting_rule：审批上线 ----
    @mcp.tool(annotations=annotations_for("approve_posting_rule"))
    @safe_tool
    def approve_posting_rule(
        event_type: str,
        version: int,
        approved_by: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """审批上线过账规则（退役旧 active 版本；升版不重写历史分录）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _posting.approve_posting_rule(
                conn, tenant_id=tenant, event_type=event_type, version=version,
                approved_by=approved_by)

    # ---- 30. list_posting_rules ----
    @mcp.tool(annotations=annotations_for("list_posting_rules"))
    @safe_tool
    def list_posting_rules(
        event_type: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """列出过账规则各版本与状态（draft/active/retired）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            sql = ("SELECT event_type, version, status, approved_by, approved_at "
                   "FROM posting_rules WHERE tenant_id=%s")
            params: list[Any] = [tenant]
            if event_type:
                sql += " AND event_type=%s"
                params.append(event_type)
            sql += " ORDER BY event_type, version"
            return paginate(fetch_all(conn, sql, tuple(params)))

    # ---- 31. post_event：事件 → 分录 ----
    @mcp.tool(annotations=annotations_for("post_event"))
    @safe_tool
    def post_event(
        event_id: int,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """按生效规则把已接受事件过一次账；缺规则抛 POSTING_RULE_MISSING 并挂待补。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _posting.post_event(conn, settings=settings, tenant_id=tenant,
                                      event_id=event_id)

    # ---- 32. list_posting_gaps：待补清单 ----
    @mcp.tool(annotations=annotations_for("list_posting_gaps"))
    @safe_tool
    def list_posting_gaps(
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """列出缺规则待补清单：哪些事件在等哪条规则。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            out = _posting.list_posting_gaps(conn, tenant_id=tenant)
            return {"data": out["items"], "completeness": out["completeness"]}

    # ---- 33-37. 期间关账（add-period-close）----
    #
    # 关账是账本的**时间轴**，不是某个板块的竖井：期间台账（close/reopen 只追加）、
    # 落账闸（BEFORE INSERT 触发器）、关账快照都在内核库里。这几个工具只做读写封装，
    # 顺序关账/只重开最近一期/快照复算等判断全在 periods 模块，工具层不重复。

    @mcp.tool(annotations=annotations_for("close_period"))
    @safe_tool
    def close_period(
        period: str,
        actor: str,
        note: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """关账（YYYY-MM）：顺序关账 + 写快照；已关重复调用幂等。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _periods.close(conn, settings=settings, tenant_id=tenant,
                                  period=period, actor=actor, note=note)

    @mcp.tool(annotations=annotations_for("reopen_period"))
    @safe_tool
    def reopen_period(
        period: str,
        actor: str,
        reason: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """重开期间：只认最近一个已关期间，且必须给理由（追加记录，不覆盖历史）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _periods.reopen(conn, settings=settings, tenant_id=tenant,
                                   period=period, actor=actor, reason=reason)

    @mcp.tool(annotations=annotations_for("period_status"))
    @safe_tool
    def period_status(
        period: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """查某期间是否关账 + 最近一次动作（谁、何时、理由、快照摘要）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _periods.status(conn, tenant, period)

    @mcp.tool(annotations=annotations_for("period_report"))
    @safe_tool
    def period_report(
        period: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """关账快照 vs 当前复算：一致 verified=true；不一致逐科目点名差额。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            return _periods.report(conn, settings=settings, tenant_id=tenant,
                                   period=period)

    @mcp.tool(annotations=annotations_for("list_periods"))
    @safe_tool
    def list_periods(
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """列出有记录的期间及其当前状态（按期间升序）。"""
        with connection(settings) as conn:
            tenant = resolve_tenant(conn, settings, tenant_id)
            rows = _periods.list_periods(conn, tenant)
            return {"data": rows, "rows_returned": len(rows), "ok": True}
