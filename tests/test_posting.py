"""过账层：规则版本化/审批门/确定性映射/缺规则挂待补/幂等/升版不重写历史。

对真实 Postgres 跑。红绿自证：删掉 posting.py 里任一条守卫，对应用例必须变红。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fd_erp_ledger_mcp import events, posting
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.ledger import compute_balance

TENANT = "acme"

GRN_RULE = {
    "lines": [
        {"account_code": "1405", "direction": "debit", "amount_from": "payload.cost"},
        {"account_code": "2202", "direction": "credit", "amount_from": "payload.cost"},
    ]
}


def _receive(conn, settings, stable_id="grn-1", cost=38400, qty=120):
    return events.append_event(
        conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
        stable_id=stable_id, occurred_at=datetime.now(timezone.utc),
        payload={"sku": "SKU-1", "quantity": qty, "unit": "pcs",
                 "warehouse": "WH1", "lot": "L2301", "cost": cost},
    )


def _activate(conn, tenant=TENANT, rule=GRN_RULE, event_type="goods_received"):
    drafted = posting.submit_posting_rule(conn, tenant_id=tenant, event_type=event_type,
                                          rule=rule, actor="cfg-eng")
    return posting.approve_posting_rule(conn, tenant_id=tenant, event_type=event_type,
                                        version=drafted["version"], approved_by="cfo")


def _bal(conn, settings, account):
    rows = compute_balance(conn, TENANT, account, settings=settings)
    # NUMERIC 由 psycopg 读成 Decimal；测试里统一成 float 再比
    return float(rows[0]["balance"]) if rows else 0.0


def test_missing_rule_parks_event_and_leaves_ledger_untouched(settings):
    """缺规则不猜科目：事件进待补清单，余额零变化。"""
    with connection(settings) as conn:
        ev = _receive(conn, settings)
        with pytest.raises(ErpError) as ei:
            posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
        assert ei.value.code == "POSTING_RULE_MISSING"
        assert ei.value.detail["event_type"] == "goods_received"
        gaps = posting.list_posting_gaps(conn, tenant_id=TENANT)
        assert [g["stable_id"] for g in gaps["items"]] == ["grn-1"]
        assert _bal(conn, settings, "1405") == 0


def test_draft_rule_does_not_post_until_approved(settings):
    with connection(settings) as conn:
        posting.submit_posting_rule(conn, tenant_id=TENANT, event_type="goods_received",
                                   rule=GRN_RULE)
        ev = _receive(conn, settings)
        with pytest.raises(ErpError) as ei:
            posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
        assert ei.value.code == "POSTING_RULE_MISSING"


def test_approved_rule_posts_deterministically_with_balanced_entries(settings):
    with connection(settings) as conn:
        _activate(conn)
        ev = _receive(conn, settings)
        out = posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
        assert out["posted"] is True and out["lines"] == 2
        assert _bal(conn, settings, "1405") == 38400
        assert _bal(conn, settings, "2202") == -38400
        # 追溯链：事件 → 事务
        link = conn.execute("SELECT txn_id, lines FROM posting_links WHERE event_id=%s",
                            (ev["id"],)).fetchone()
        assert link[0] == out["txn_id"] and link[1] == 2
        # 缺口被解除
        assert posting.list_posting_gaps(conn, tenant_id=TENANT)["items"] == []


def test_posting_is_idempotent(settings):
    with connection(settings) as conn:
        _activate(conn)
        ev = _receive(conn, settings)
        first = posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
        again = posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
        assert again["duplicate"] is True
        assert again["txn_id"] == first["txn_id"]
        assert _bal(conn, settings, "1405") == 38400  # 未翻倍
        assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 2


def test_rule_upgrade_does_not_rewrite_history(settings):
    """升版只影响其后事件；已落账分录逐字不变。"""
    with connection(settings) as conn:
        _activate(conn)
        first = _receive(conn, settings, stable_id="grn-old", cost=100)
        posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=first["id"])
        before = conn.execute(
            "SELECT account_code, direction, amount FROM journal_entries ORDER BY line_no").fetchall()

        _activate(conn, rule={"lines": [
            {"account_code": "1405", "direction": "debit", "amount_from": "payload.cost"},
            {"account_code": "1300", "direction": "credit", "amount_from": "payload.cost"},
        ]})
        second = _receive(conn, settings, stable_id="grn-new", cost=200)
        posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=second["id"])

        after_old = conn.execute(
            "SELECT account_code, direction, amount FROM journal_entries "
            "WHERE stable_id LIKE 'grn-old%' ORDER BY line_no").fetchall()
        assert after_old == before
        assert _bal(conn, settings, "1300") == -200


def test_rule_rejects_accounts_outside_machine_lane(settings):
    """现金/预收/权益不在机器车道白名单：执行面与资金面归外部系统。"""
    with connection(settings) as conn:
        with pytest.raises(ErpError) as ei:
            posting.submit_posting_rule(conn, tenant_id=TENANT, event_type="goods_received",
                                       rule={"lines": [
                                           {"account_code": "1000", "direction": "debit",
                                            "amount_from": "payload.cost"},
                                           {"account_code": "4000", "direction": "credit",
                                            "amount_from": "payload.cost"}]})
        assert "白名单" in ei.value.message
        assert "1000" in (ei.value.detail or {}).get("forbidden_accounts", [])


def test_rule_accepts_the_chart_codes_for_inventory_and_cogs(settings):
    """通用科目 1200 存货 / 2000 应付 / 5000 营业成本必须在允许集内。

    回归：旧白名单只有计费车道的 1405/2202/6401（都不在 subjects 预设里），
    于是「到货过账」写到了按 1200 取数时看不见的科目上——分录有、余额是零。
    """
    with connection(settings) as conn:
        drafted = posting.submit_posting_rule(
            conn, tenant_id=TENANT, event_type="goods_received",
            rule={"lines": [
                {"account_code": "1200", "direction": "debit",
                 "amount_from": "payload.cost"},
                {"account_code": "2000", "direction": "credit",
                 "amount_from": "payload.cost"}]})
        assert drafted["version"]
        cogs = posting.submit_posting_rule(
            conn, tenant_id=TENANT, event_type="goods_issued",
            rule={"lines": [
                {"account_code": "5000", "direction": "debit",
                 "amount_from": "payload.cost"},
                {"account_code": "1200", "direction": "credit",
                 "amount_from": "payload.cost"}]})
        assert cogs["version"]
        # 红线不变：现金/预收/权益仍不可由规则落账
        with pytest.raises(ErpError) as ei:
            posting.submit_posting_rule(
                conn, tenant_id=TENANT, event_type="goods_received",
                rule={"lines": [
                    {"account_code": "2100", "direction": "debit",
                     "amount_from": "payload.cost"},
                    {"account_code": "4000", "direction": "credit",
                     "amount_from": "payload.cost"}]})
        assert "2100" in (ei.value.detail or {}).get("forbidden_accounts", [])


def test_rule_requires_both_sides_and_amount_source(settings):
    with connection(settings) as conn:
        with pytest.raises(ErpError):
            posting.submit_posting_rule(conn, tenant_id=TENANT, event_type="goods_received",
                                       rule={"lines": [
                                           {"account_code": "1405", "direction": "debit",
                                            "amount_from": "payload.cost"}]})
        with pytest.raises(ErpError):
            posting.submit_posting_rule(conn, tenant_id=TENANT, event_type="goods_received",
                                       rule={"lines": [
                                           {"account_code": "1405", "direction": "debit"},
                                           {"account_code": "2202", "direction": "credit",
                                            "amount_from": "1"}]})


def test_only_one_active_rule_per_event_type(settings):
    with connection(settings) as conn:
        _activate(conn)
        v2 = posting.submit_posting_rule(conn, tenant_id=TENANT, event_type="goods_received",
                                        rule=GRN_RULE)
        posting.approve_posting_rule(conn, tenant_id=TENANT, event_type="goods_received",
                                    version=v2["version"], approved_by="cfo")
        active = conn.execute("SELECT COUNT(*) FROM posting_rules "
                              "WHERE status='active' AND event_type='goods_received'").fetchone()[0]
        assert active == 1
        retired = conn.execute("SELECT COUNT(*) FROM posting_rules "
                               "WHERE status='retired'").fetchone()[0]
        assert retired == 1


def test_rule_referencing_missing_payload_field_is_an_error_not_a_guess(settings):
    with connection(settings) as conn:
        _activate(conn, rule={"lines": [
            {"account_code": "1405", "direction": "debit", "amount_from": "payload.no_such"},
            {"account_code": "2202", "direction": "credit", "amount_from": "payload.no_such"},
        ]})
        ev = _receive(conn, settings)
        with pytest.raises(ErpError) as ei:
            posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
        assert ei.value.code == "POSTING_RULE_MISSING"
        assert ei.value.detail["missing_field"] == "no_such"
        assert _bal(conn, settings, "1405") == 0


def test_event_type_without_posting_requirement_is_skipped_honestly(settings):
    """invoice_requested 明确不需要过账：返回 not_required，不挂缺口。"""
    with connection(settings) as conn:
        ev = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="invoice_requested",
            stable_id="inv-1", occurred_at=datetime.now(timezone.utc),
            payload={"invoice_ref": "INV-0001"})
        out = posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
        assert out == {"posted": False, "reason": "not_required",
                       "event_id": ev["id"], "event_type": "invoice_requested"}
        assert posting.list_posting_gaps(conn, tenant_id=TENANT)["items"] == []


def test_pending_event_cannot_be_posted(settings):
    with connection(settings) as conn:
        _activate(conn)
        ev = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
            stable_id="grn-draft", occurred_at=datetime.now(timezone.utc), lane="draft",
            payload={"sku": "SKU-1", "quantity": 10, "unit": "pcs",
                     "warehouse": "WH1", "cost": 100})
        with pytest.raises(ErpError) as ei:
            posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
        assert ei.value.code == "EVENT_STATE"


# ---------------- 3.5 事件流 vs 分录流对账（内部两源） ----------------

def _reconcile_events(settings, tenant=TENANT, event_types=None):
    from fd_erp_ledger_mcp.recon.engine import enqueue, wait_for_task
    types = event_types or ["goods_received"]
    job = enqueue(settings, tenant,
                  {"type": "events", "event_types": types, "amount_from": "payload.cost"},
                  {"type": "posting", "event_types": types},
                  None)
    return wait_for_task(settings, tenant, job["task_id"], timeout=30.0)


def test_event_vs_posting_recon_clean_when_consistent(settings):
    """事件与分录一致时差异清单为空，且两源都被读到（不是假绿的空对空）。"""
    with connection(settings) as conn:
        _activate(conn)
        ev = _receive(conn, settings, cost=500)
        posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
    rep = _reconcile_events(settings)
    assert rep["status"] == "done"
    result = rep["result"]
    assert result["diffs"] == []
    assert result["summary"]["left_unique"] == 1
    assert result["summary"]["right_unique"] == 1


def test_event_vs_posting_recon_flags_event_without_entry(settings):
    """有事件无分录 → missing_in_right，且带双侧引用（右侧为空也要说明）。"""
    with connection(settings) as conn:
        _receive(conn, settings, cost=500)  # 不 post
    rep = _reconcile_events(settings)
    diffs = rep["result"]["diffs"]
    assert [d["diff_type"] for d in diffs] == ["missing_in_right"]
    assert diffs[0]["left_ref"].startswith("event:goods_received:")


def test_event_vs_posting_recon_flags_amount_mismatch(settings):
    """事件声明 500、规则落 400 → amount_mismatch 暴露，不被静默掩盖。"""
    with connection(settings) as conn:
        _activate(conn, rule={"lines": [
            {"account_code": "1405", "direction": "debit", "amount_from": "400"},
            {"account_code": "2202", "direction": "credit", "amount_from": "400"},
        ]})
        ev = _receive(conn, settings, cost=500)
        posting.post_event(conn, settings=settings, tenant_id=TENANT, event_id=ev["id"])
    rep = _reconcile_events(settings)
    diffs = rep["result"]["diffs"]
    assert [d["diff_type"] for d in diffs] == ["amount_mismatch"]
    assert diffs[0]["left_ref"] and diffs[0]["right_ref"]


def test_internal_source_rejects_foreign_connection(settings):
    """内部两源不接受外部声明的连接：否则调用方可指定去对哪本账。"""
    from fd_erp_ledger_mcp.errors import ErpError
    from fd_erp_ledger_mcp.recon.adapters import build_adapter
    with pytest.raises(ErpError) as ei:
        build_adapter({"type": "events", "records": []}, "left")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "引擎注入" in ei.value.message
