"""信用额度（add-supply-chain-vertical 任务 4.2）。

覆盖验收：信用占用释放回冲 / 额度不足拦截（CREDIT_EXCEEDED 带明细）/ 额度口径
账本衍生（1300 分录重算）/ 占用不写复式分录（备忘性质）/ 幂等重推 / 跨租户拒绝。
"""
from __future__ import annotations

import pytest

from fd_erp_ledger_mcp.credit import (credit_position, release_credit,
                                      reserve_credit, set_credit_limit)
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.ledger import query_entries
from fd_erp_ledger_mcp.proposals import confirm_proposal, submit_proposal

TENANT = "acme"


def _register(conn, settings, customer="cust-1", limit=100):
    return set_credit_limit(conn, tenant_id=TENANT, customer_id=customer,
                            limit_amount=limit, currency="CNY",
                            source_ref="credit-policy-2026")


def _post_ar(conn, settings, stable, amount):
    """落一笔 1300 应收（走既有 proposal 确认车道）。"""
    submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id=stable,
                    lines=[{"account_code": "1300", "direction": "debit", "amount": amount,
                            "currency": "CNY"},
                           {"account_code": "4000", "direction": "credit", "amount": amount,
                            "currency": "CNY"}])
    confirm_proposal(conn, settings=settings, tenant_id=TENANT, stable_id=stable)


# --------------------------- 占用 / 释放 回冲 ---------------------------

def test_reserve_release_recomputes_available(settings):
    with connection(settings) as conn:
        _register(conn, settings)
        r1 = reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                            stable_id="res-1", amount=60, currency="CNY",
                            source_ref="order-A")
        assert r1["ok"] is True and r1["duplicate"] is False
        assert r1["available_after"] == "40.000000"  # 100 − 60
        # 释放 → 可用回冲到 100
        rel = release_credit(conn, settings=settings, tenant_id=TENANT,
                             customer_id="cust-1", stable_id="res-1")
        assert rel["ok"] is True and rel["released"] is True
        assert rel["available_after"] == "100.000000"
        # 释放幂等：重复释放返回首次结果、可用不变
        rel2 = release_credit(conn, settings=settings, tenant_id=TENANT,
                              customer_id="cust-1", stable_id="res-1")
        assert rel2["already_released"] is True
        assert rel2["available_after"] == "100.000000"


def test_reserve_idempotent_same_stable_id(settings):
    with connection(settings) as conn:
        _register(conn, settings)
        r1 = reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                            stable_id="res-idem", amount=30)
        r2 = reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                            stable_id="res-idem", amount=99)  # 金额不同也只认首次
        assert r2["duplicate"] is True and r2["ok"] is True
        assert r2["reservation_id"] == r1["reservation_id"]
        pos = credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1")
        assert pos["reserved"] == "30.000000"  # 未重复占用


def test_credit_exceeded_blocked_with_detail(settings):
    with connection(settings) as conn:
        _register(conn, settings, limit=100)
        reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                       stable_id="res-1", amount=60)
        with pytest.raises(ErpError) as ei:
            reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                           stable_id="res-2", amount=50)  # 可用仅 40
        assert ei.value.code == "CREDIT_EXCEEDED"
        d = ei.value.detail
        assert d["limit"] == "100.000000"
        assert d["reserved"] == "60.000000"
        assert d["available"] == "40.000000"
        assert d["requested"] == "50.000000"
        # 占用表未新增被拒行
        pos = credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1")
        assert pos["reserved"] == "60.000000"


# --------------------------- 额度口径 = 账本衍生 ---------------------------

def test_available_derives_from_ledger_ar(settings):
    with connection(settings) as conn:
        _register(conn, settings, limit=100)
        pos0 = credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1")
        assert pos0["ar_balance"] == "0.000000"
        assert pos0["available"] == "100.000000"
        # 落一笔 30 应收（1300 借）→ available = 100 − 30
        _post_ar(conn, settings, "ar-1", 30)
        pos1 = credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1")
        assert pos1["ar_balance"] == "30.000000"
        assert pos1["available"] == "70.000000"
        # 占用 20 → available = 70 − 20
        reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                       stable_id="res-ar", amount=20)
        pos2 = credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1")
        assert pos2["reserved"] == "20.000000"
        assert pos2["available"] == "50.000000"
        assert pos2["ar_account"] == "1300"


def test_reservation_is_memo_not_double_entry(settings):
    with connection(settings) as conn:
        _register(conn, settings)
        reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                       stable_id="res-memo", amount=40)
        release_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                       stable_id="res-memo")
        # 占用/释放 MUST NOT 产生任何复式分录
        assert query_entries(conn, TENANT) == []


# --------------------------- 守界与跨租户 ---------------------------

def test_unregistered_customer_and_currency_guard(settings):
    with connection(settings) as conn:
        with pytest.raises(ErpError) as ei:
            reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="ghost",
                           stable_id="res-x", amount=10)
        assert ei.value.code == "NOT_FOUND"
        with pytest.raises(ErpError) as ei2:
            credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="ghost")
        assert ei2.value.code == "NOT_FOUND"
        # 跨币种额度不在内核口径（避免无出处折算），如实拒绝
        set_credit_limit(conn, tenant_id=TENANT, customer_id="cust-usd",
                         limit_amount=100, currency="USD")
        with pytest.raises(ErpError) as ei3:
            reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-usd",
                           stable_id="res-usd", amount=10, currency="USD")
        assert ei3.value.code == "INVALID_ARGUMENT"


def test_cross_tenant_denied(settings):
    with connection(settings) as conn:
        _register(conn, settings)
        from fd_erp_ledger_mcp.tenancy import resolve_tenant
        with pytest.raises(ErpError) as ei:
            resolve_tenant(conn, settings, "other-corp")
        assert ei.value.code == "TENANT_DENIED"
        # other-corp 名下无任何额度事实
        from fd_erp_ledger_mcp.credit import ar_balance, reserved_total
        assert ar_balance(conn, "other-corp") == 0
        assert reserved_total(conn, "other-corp", "cust-1") == 0
