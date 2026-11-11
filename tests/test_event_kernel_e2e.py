"""串行全链真跑 + 回滚演练（change: establish-erp-event-kernel 10.1-10.3）。

三条都是「整条链」级别的断言，不是单元级形状检查：

  10.1 串行全链：外部到货事实 → 事件（导入车道）→ 对象余额 → 过账规则 → 分录
       → 事件-分录对账 → 差异清单为空。六步各留可复核产物。
  10.2 拟稿全链：agent 提交库存调整 → 待确认（余额不变）→ 确认 → 余额变化；
       另一条被拒 → 余额不变。
  10.3 回滚演练：完全不碰事件/对象/过账三张新表时，老账本链路
       （submit→confirm→balance→recon→reverse）行为不变，且新表保持为空——
       证明没有隐藏耦合，停用新工具组就能回到旧行为。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fd_erp_ledger_mcp import events, posting
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.ledger import compute_balance
from fd_erp_ledger_mcp.proposals import (confirm_proposal, submit_proposal)
from fd_erp_ledger_mcp.recon.engine import enqueue, wait_for_task

TENANT = "acme"
GRN_LINES = [
    {"account_code": "1405", "direction": "debit", "amount_from": "payload.cost"},
    {"account_code": "2202", "direction": "credit", "amount_from": "payload.cost"},
]


def _bal(conn, settings, account):
    rows = compute_balance(conn, TENANT, account, settings=settings)
    return float(rows[0]["balance"]) if rows else 0.0


def _received(conn, settings, *, stable_id="grn-1", qty=120, cost=38400,
              warehouse="WH1", lot="L2301", lane="ingest"):
    return events.append_event(
        conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
        stable_id=stable_id, occurred_at=datetime.now(timezone.utc), lane=lane,
        payload={"sku": "SKU-1", "quantity": qty, "unit": "pcs",
                 "warehouse": warehouse, "lot": lot, "cost": cost})


def test_10_1_serial_full_chain_from_external_fact_to_recon(settings):
    """六步串行真跑，每步产物都可复核。"""
    with connection(settings) as conn:
        # ① 外部事实落事件（导入车道：来源系统即权威，写入即事实）
        ev = _received(conn, settings)
        assert ev["status"] == "accepted"

        # ② 数量面投影
        bal = events.project_object_event(
            conn, tenant_id=TENANT, event_id=ev["id"], object_type="sku",
            object_key="SKU-1", quantity_delta=120, unit="pcs",
            dimension={"warehouse": "WH1", "lot": "L2301"})
        assert bal["quantity"] == 120 and bal["drift"] is False

        # ③ 过账规则（草稿 → 审批上线）
        drafted = posting.submit_posting_rule(
            conn, tenant_id=TENANT, event_type="goods_received",
            rule={"lines": GRN_LINES}, actor="cfg-eng")
        posting.approve_posting_rule(conn, tenant_id=TENANT,
                                    event_type="goods_received",
                                    version=drafted["version"], approved_by="cfo")

        # ④ 过账 → 分录 + 追溯链
        out = posting.post_event(conn, settings=settings, tenant_id=TENANT,
                                 event_id=ev["id"])
        assert out["posted"] is True and out["lines"] == 2
        link = conn.execute("SELECT txn_id FROM posting_links WHERE event_id=%s",
                            (ev["id"],)).fetchone()
        assert link[0] == out["txn_id"]

        # ⑤ 金额面与数量面各自成立、互不冒充
        assert _bal(conn, settings, "1405") == 38400.0
        assert _bal(conn, settings, "2202") == -38400.0
        columns = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='journal_entries'").fetchall()]
        assert "quantity" not in columns  # 数量没混进账本

    # ⑥ 事件-分录对账：两源一致，差异清单为空
    job = enqueue(settings, TENANT,
                  {"type": "events", "event_types": ["goods_received"],
                   "amount_from": "payload.cost"},
                  {"type": "posting", "event_types": ["goods_received"]}, None)
    rep = wait_for_task(settings, TENANT, job["task_id"], timeout=30.0)
    assert rep["status"] == "done"
    assert rep["result"]["diffs"] == []
    assert rep["result"]["summary"]["matched"] == 1


def test_10_2_draft_chain_confirm_changes_balance_only_after_gate(settings):
    """拟稿链：确认前余额零变化，确认后才变化；被拒的永不生效。"""
    with connection(settings) as conn:
        drafted = posting.submit_posting_rule(
            conn, tenant_id=TENANT, event_type="goods_adjusted",
            rule={"lines": [
                {"account_code": "1405", "direction": "debit", "amount_from": "payload.cost"},
                {"account_code": "5200", "direction": "credit", "amount_from": "payload.cost"},
            ]})
        posting.approve_posting_rule(conn, tenant_id=TENANT,
                                    event_type="goods_adjusted",
                                    version=drafted["version"], approved_by="cfo")

        ok = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_adjusted",
            stable_id="adj-ok", occurred_at=datetime.now(timezone.utc), lane="draft",
            payload={"sku": "SKU-1", "quantity": 5, "unit": "pcs",
                     "warehouse": "WH1", "reason": "盘盈", "cost": 1600})
        bad = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_adjusted",
            stable_id="adj-bad", occurred_at=datetime.now(timezone.utc), lane="draft",
            payload={"sku": "SKU-1", "quantity": 5, "unit": "pcs",
                     "warehouse": "WH1", "reason": "无依据", "cost": 9999})

        # 待确认：投影与过账都必须拒（未过门不算事实）
        assert ok["status"] == "pending"
        for ev in (ok, bad):
            try:
                events.project_object_event(
                    conn, tenant_id=TENANT, event_id=ev["id"], object_type="sku",
                    object_key="SKU-1", quantity_delta=5, unit="pcs",
                    dimension={"warehouse": "WH1"})
                raise AssertionError("待确认事件不该能投影")
            except ErpError as exc:
                assert exc.code == "EVENT_STATE"
        assert _bal(conn, settings, "1405") == 0.0

        # 确认一条、拒绝一条
        events.decide_event(conn, tenant_id=TENANT, stable_id="adj-ok", lane="draft",
                            decision="accepted", actor="ops")
        events.decide_event(conn, tenant_id=TENANT, stable_id="adj-bad", lane="draft",
                            decision="rejected", actor="ops", note="无依据")
        posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ok["id"])
        assert _bal(conn, settings, "1405") == 1600.0
        try:
            posting.post_event(conn, settings=settings, tenant_id=TENANT,
                               event_id=bad["id"])
            raise AssertionError("被拒事件不该能过账")
        except ErpError as exc:
            assert exc.code == "EVENT_STATE"
        assert _bal(conn, settings, "1405") == 1600.0  # 被拒的那笔零影响


def test_10_3_legacy_ledger_path_untouched_when_event_tools_unused(settings):
    """回滚演练：不碰新表时，老账本链路行为不变，新表保持为空。

    这正是「停用 event/object/posting 工具组即可回到旧行为」的机器化断言：
    老链路一旦隐式依赖新表，本用例就会红。
    """
    with connection(settings) as conn:
        # 老链路的做法一字未改：submit → confirm → balance
        p = submit_proposal(conn, settings=settings, tenant_id=TENANT,
                            stable_id="legacy-1",
                            lines=[{"account_code": "1405", "direction": "debit",
                                    "amount": 500},
                                   {"account_code": "2202", "direction": "credit",
                                    "amount": 500}])
        assert p["status"] == "pending"
        confirmed = confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                                     stable_id="legacy-1", confirmed_by="human")
        assert confirmed["status"] == "confirmed"
        assert _bal(conn, settings, "1405") == 500.0

        # 老链路跑完，四张新表必须一条不写（没有隐藏耦合）
        for table in ("business_events", "object_events", "object_balances",
                      "posting_links"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 0, f"{table} 被老链路写入 {n} 行：存在隐藏耦合"

        # 老对账（inline 两源）照旧可用，且不依赖事件源
        job = enqueue(settings, TENANT,
                      {"type": "inline", "records": [
                          {"stable_id": "a", "amount": "10", "status": "posted"}]},
                      {"type": "inline", "records": [
                          {"stable_id": "a", "amount": "10", "status": "posted"}]},
                      None)
        rep = wait_for_task(settings, TENANT, job["task_id"], timeout=30.0)
        assert rep["status"] == "done" and rep["result"]["diffs"] == []
