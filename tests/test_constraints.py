"""三层兜底 · 约束层 + 车道层 + 幂等层（6.1），以及多币种/冲正（5.1/5.2）。

全部对真实 Postgres 跑：DB 约束层不可 mock。
"""
from __future__ import annotations

import psycopg
import pytest

from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.fx import seed_fx_facts
from fd_erp_ledger_mcp.ledger import compute_balance, query_entries
from fd_erp_ledger_mcp.proposals import (confirm_proposal, list_proposals,
                                         reject_proposal, submit_proposal)

TENANT = "acme"


def _conn(settings):
    return connection(settings)


def _cny_lines(amount=100):
    return [
        {"account_code": "cash", "direction": "debit", "amount": amount, "currency": "CNY"},
        {"account_code": "revenue", "direction": "credit", "amount": amount, "currency": "CNY"},
    ]


# --------------------------- 约束层 ---------------------------

def test_unbalanced_entry_rejected_by_db(settings):
    """不平分录被确认 → 数据库约束拒绝，账本零变化，proposal 仍 pending。"""
    with _conn(settings) as conn:
        p = submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="unb-1",
                            lines=[{"account_code": "cash", "direction": "debit", "amount": 100},
                                   {"account_code": "revenue", "direction": "credit", "amount": 90}])
        assert p["status"] == "pending"
        with pytest.raises(ErpError) as ei:
            confirm_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="unb-1")
        assert ei.value.code == "UNBALANCED"
        # 账本零变化
        assert query_entries(conn, TENANT) == []
        # proposal 仍 pending（确认失败未提交状态）
        props = list_proposals(conn, TENANT)
        assert props[0]["status"] == "pending"


def test_append_only_update_and_delete_rejected(settings):
    with _conn(settings) as conn:
        submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="ao-1",
                        lines=_cny_lines())
        confirm_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="ao-1")
    # 直接用原始连接尝试改/删已落账分录 —— 必须失败
    raw = psycopg.connect(settings.database_url, autocommit=True)
    try:
        with pytest.raises(psycopg.errors.Error) as ei:
            raw.execute("UPDATE journal_entries SET amount=999 WHERE tenant_id=%s", (TENANT,))
        assert "ERP_APPEND_ONLY" in str(ei.value)
        with pytest.raises(psycopg.errors.Error) as ei2:
            raw.execute("DELETE FROM journal_entries WHERE tenant_id=%s", (TENANT,))
        assert "ERP_APPEND_ONLY" in str(ei2.value)
        # TRUNCATE 也拒绝（无维护开关）
        with pytest.raises(psycopg.errors.Error) as ei3:
            raw.execute("TRUNCATE journal_entries")
        assert "ERP_APPEND_ONLY" in str(ei3.value)
    finally:
        raw.close()


# --------------------------- 车道层 ---------------------------

def test_proposal_pending_does_not_change_ledger(settings):
    with _conn(settings) as conn:
        submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="lane-1",
                        lines=_cny_lines(250))
        assert compute_balance(conn, TENANT, None, settings=settings) == []
        assert query_entries(conn, TENANT) == []


def test_confirm_posts_and_balance_recomputes(settings):
    with _conn(settings) as conn:
        submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="lane-2",
                        lines=_cny_lines(300))
        res = confirm_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="lane-2")
        assert res["status"] == "confirmed"
        entries = query_entries(conn, TENANT)
        assert len(entries) == 2
        # 余额可重算：cash 借 300，revenue 贷 300
        bal = {r["account_code"]: r for r in compute_balance(conn, TENANT, None, settings=settings)}
        assert bal["cash"]["balance"] == "300.000000"
        assert bal["revenue"]["balance"] == "-300.000000"


def test_reject_leaves_trail_no_posting(settings):
    with _conn(settings) as conn:
        submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="lane-rej",
                        lines=_cny_lines())
        reject_proposal(conn, tenant_id=TENANT, stable_id="lane-rej", reason="looks wrong")
        assert query_entries(conn, TENANT) == []
        props = list_proposals(conn, TENANT)
        assert props[0]["status"] == "rejected"
        audit = conn.execute(
            "SELECT count(*) FROM audit WHERE action='proposal_rejected'").fetchone()[0]
        assert audit == 1


# --------------------------- 幂等层 ---------------------------

def test_duplicate_submit_returns_first_result(settings):
    with _conn(settings) as conn:
        p1 = submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="idem-1",
                             lines=_cny_lines())
        p2 = submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="idem-1",
                             lines=_cny_lines())
        assert p1["proposal_id"] == p2["proposal_id"]
        assert p2["idempotent_hit"] is True
        assert len(list_proposals(conn, TENANT)) == 1


def test_reconfirm_is_idempotent(settings):
    with _conn(settings) as conn:
        submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="idem-2",
                        lines=_cny_lines(120))
        r1 = confirm_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="idem-2")
        r2 = confirm_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="idem-2")
        assert r2["status"] == "already_confirmed"
        assert r2["txn_id"] == r1["txn_id"]
        assert len(query_entries(conn, TENANT)) == 2  # 未重复入账


# --------------------------- 多币种（5.1）---------------------------

def test_fx_unsourced_rejected(settings):
    with _conn(settings) as conn:
        # 未在 fx_facts 登记出处 → 拒绝
        with pytest.raises(ErpError) as ei:
            submit_proposal(
                conn, settings=settings, tenant_id=TENANT, stable_id="fx-1",
                lines=[
                    {"account_code": "cash", "direction": "debit", "amount": 100,
                     "currency": "USD", "fx_rate": "7.1", "fx_as_of": "2026-11-07",
                     "fx_source": "ecb-exr"},
                    {"account_code": "revenue", "direction": "credit", "amount": 100,
                     "currency": "USD", "fx_rate": "7.1", "fx_as_of": "2026-11-07",
                     "fx_source": "ecb-exr"},
                ])
        assert ei.value.code == "FX_UNSOURCED"


def test_fx_sourced_dual_record(settings):
    with _conn(settings) as conn:
        # 从 ecb-exr 观测流做种
        seeded = seed_fx_facts(conn, [
            {"quote_currency": "USD", "rate": "7.1000000000", "as_of": "2026-11-07",
             "source": "ecb-exr"},
            {"quote_currency": "EUR", "rate": "", "as_of": "2026-11-07", "source": ""},
        ], settings=settings)
        assert seeded["seeded"] == 1
        assert len(seeded["rejected"]) == 1  # 无出处拒绝

        lines = [
            {"account_code": "cash", "direction": "debit", "amount": 100, "currency": "USD",
             "fx_rate": "7.1000000000", "fx_as_of": "2026-11-07", "fx_source": "ecb-exr"},
            {"account_code": "revenue", "direction": "credit", "amount": 100, "currency": "USD",
             "fx_rate": "7.1000000000", "fx_as_of": "2026-11-07", "fx_source": "ecb-exr"},
        ]
        submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="fx-2", lines=lines)
        confirm_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="fx-2")
        entries = query_entries(conn, TENANT)
        e = entries[0]
        assert e["currency"] == "USD"
        assert e["amount"] == "100.000000"
        assert e["amount_base"] == "710.000000"
        assert e["fx_rate"] == "7.1000000000"
        assert e["fx_source"] == "ecb-exr"
        assert str(e["fx_as_of"]) == "2026-11-07"


# --------------------------- 冲正（5.2）---------------------------

def test_reversal_links_original_and_keeps_it(settings):
    with _conn(settings) as conn:
        submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="orig-1",
                        lines=_cny_lines(400))
        posted = confirm_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="orig-1")
        orig_txn = posted["txn_id"]
        # 发起冲正
        submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="rev-1",
                        kind="reversal", reversal_of=orig_txn, lines=[])
        confirm_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="rev-1")
        entries = query_entries(conn, TENANT)
        orig = [e for e in entries if e["txn_id"] == orig_txn]
        rev = [e for e in entries if e["is_reversal"] and e["reversal_of"] == orig_txn]
        assert len(orig) == 2  # 原分录原样保留
        assert len(rev) == 2
        # 原分录方向未变
        assert {e["direction"] for e in orig} == {"debit", "credit"}
        # 冲正后余额归零
        bal = {r["account_code"]: r["balance"]
               for r in compute_balance(conn, TENANT, None, settings=settings)}
        assert bal["cash"] == "0.000000"


# --------------------------- 租户闸（3.1）---------------------------

def test_cross_tenant_query_rejected_and_audited(settings):
    from fd_erp_ledger_mcp.tenancy import resolve_tenant
    with _conn(settings) as conn:
        with pytest.raises(ErpError) as ei:
            resolve_tenant(conn, settings, "other-corp")
        assert ei.value.code == "TENANT_DENIED"
        n = conn.execute(
            "SELECT count(*) FROM audit WHERE action='tenant_denied'").fetchone()[0]
        assert n == 1
