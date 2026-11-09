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


def _exp(days: int = 7) -> str:
    """默认失效时点（占用必填，服务端不填隐藏默认值）。"""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _reserve(conn, settings, **kw):
    """测试侧统一入口：只补一个合理的 expires_at，不改被测语义。"""
    kw.setdefault("expires_at", _exp())
    return reserve_credit(conn, settings=settings, tenant_id=TENANT, **kw)


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
        r1 = _reserve(conn, settings, customer_id="cust-1",
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
        # 重试必须重发同一个失效时点：同键不同时点不是重试，是"延长寿命"，
        # 会被拒绝（见 test_reserve_same_key_different_expiry_is_refused）。
        same_expiry = _exp()
        r1 = _reserve(conn, settings, customer_id="cust-1",
                            stable_id="res-idem", amount=30, expires_at=same_expiry)
        r2 = _reserve(conn, settings, customer_id="cust-1",
                            stable_id="res-idem", amount=99,
                            expires_at=same_expiry)  # 金额不同也只认首次
        assert r2["duplicate"] is True and r2["ok"] is True
        assert r2["reservation_id"] == r1["reservation_id"]
        pos = credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1")
        assert pos["reserved"] == "30.000000"  # 未重复占用


def test_reserve_requires_expiry(settings):
    """占用没有失效时点就无法被识别为孤儿：缺参数即拒，不填隐藏默认值。"""
    with connection(settings) as conn:
        _register(conn, settings)
        with pytest.raises(ErpError) as ei:
            reserve_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                           stable_id="res-no-exp", amount=10)
        assert ei.value.code == "INVALID_ARGUMENT"
        assert "expires_at" in str(ei.value)
        rows = conn.execute("SELECT count(*) FROM credit_reservations").fetchone()
        assert rows[0] == 0, "被拒的请求不得留下占用行"


def test_reserve_same_key_different_expiry_is_refused(settings):
    """同幂等键换一个更晚的失效时点 = 用重推延长寿命，必须拒绝且不改原行。"""
    with connection(settings) as conn:
        _register(conn, settings)
        first = _exp(3)
        _reserve(conn, settings, customer_id="cust-1", stable_id="res-drift",
                 amount=10, expires_at=first)
        with pytest.raises(ErpError) as ei:
            _reserve(conn, settings, customer_id="cust-1", stable_id="res-drift",
                     amount=10, expires_at=_exp(30))
        assert ei.value.code == "INVALID_ARGUMENT"
        row = conn.execute(
            "SELECT expires_at FROM credit_reservations WHERE stable_id='res-drift'"
        ).fetchone()
        from datetime import datetime as _dt
        # 同一时刻可能以不同时区表示（库按会话时区回读）：比时间点，不比字符串。
        assert row is not None
        assert row[0].timestamp() == _dt.fromisoformat(first).timestamp()


def test_reserve_expiry_beyond_configured_ceiling_is_refused(settings):
    """超长失效时点等于规避对账：上界由部署配置给出，不是业务层硬编码。"""
    with connection(settings) as conn:
        _register(conn, settings)
        with pytest.raises(ErpError) as ei:
            _reserve(conn, settings, customer_id="cust-1", stable_id="res-too-long",
                     amount=10, expires_at=_exp(settings.credit_reservation_max_days + 1))
        assert ei.value.code == "INVALID_ARGUMENT"
        assert "上限" in str(ei.value)


def test_expired_reservation_stops_counting_but_is_not_released(settings):
    """过期 ≠ 释放：钱不再被锁，但占用仍存在、仍可处置。"""
    with connection(settings) as conn:
        _register(conn, settings)
        # 直接写一条已过期的占用（服务端不允许声明过去时间，故绕开入口构造存量态）
        conn.execute(
            "INSERT INTO credit_reservations(tenant_id, customer_id, stable_id, amount, "
            "currency, status, source_ref, provenance, expires_at, created_at) "
            "VALUES (%s,'cust-1','res-expired','25','CNY','reserved','order:SO-1',"
            "'{\"kind\":\"memo\"}'::jsonb, now() - interval '1 day', now() - interval '8 days')",
            (TENANT,),
        )
        pos = credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1")
        assert pos["reserved"] == "0.000000", "已过期占用不计入可用额度"
        status = conn.execute(
            "SELECT status FROM credit_reservations WHERE stable_id='res-expired'"
        ).fetchone()[0]
        assert status == "reserved", "过期不得被写成已释放"


def test_legacy_row_without_expiry_still_counts(settings):
    """迁移前的存量行（expires_at IS NULL）按保守口径仍计入占用。"""
    with connection(settings) as conn:
        _register(conn, settings)
        conn.execute(
            "INSERT INTO credit_reservations(tenant_id, customer_id, stable_id, amount, "
            "currency, status, source_ref, provenance) "
            "VALUES (%s,'cust-1','res-legacy','40','CNY','reserved','order:SO-0',"
            "'{\"kind\":\"memo\"}'::jsonb)",
            (TENANT,),
        )
        pos = credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1")
        assert pos["reserved"] == "40.000000"
        assert pos["available"] == "60.000000"


def test_release_records_who_and_why(settings):
    """追溯：释放必须能回答"谁释放的、凭什么"，不能只有一个时间戳。"""
    with connection(settings) as conn:
        _register(conn, settings)
        _reserve(conn, settings, customer_id="cust-1", stable_id="res-trace", amount=10)
        release_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                       stable_id="res-trace", released_by="staff:SE-007",
                       reason="customer cancelled order SO-9")
        row = conn.execute(
            "SELECT released_by, release_reason, released_at FROM credit_reservations "
            "WHERE stable_id='res-trace'"
        ).fetchone()
        assert row[0] == "staff:SE-007"
        assert "cancelled" in row[1]
        assert row[2] is not None
        # 幂等：再释放一次返回首次结果，不产生第二条处置
        again = release_credit(conn, settings=settings, tenant_id=TENANT,
                               customer_id="cust-1", stable_id="res-trace",
                               released_by="someone-else", reason="different")
        assert again["already_released"] is True
        row2 = conn.execute(
            "SELECT released_by, release_reason FROM credit_reservations "
            "WHERE stable_id='res-trace'"
        ).fetchone()
        assert row2[0] == "staff:SE-007", "重复释放不得改写首次处置记录"


def test_credit_exceeded_blocked_with_detail(settings):
    with connection(settings) as conn:
        _register(conn, settings, limit=100)
        _reserve(conn, settings, customer_id="cust-1",
                       stable_id="res-1", amount=60)
        with pytest.raises(ErpError) as ei:
            _reserve(conn, settings, customer_id="cust-1",
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
        _reserve(conn, settings, customer_id="cust-1",
                       stable_id="res-ar", amount=20)
        pos2 = credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1")
        assert pos2["reserved"] == "20.000000"
        assert pos2["available"] == "50.000000"
        assert pos2["ar_account"] == "1300"


def test_reservation_is_memo_not_double_entry(settings):
    with connection(settings) as conn:
        _register(conn, settings)
        _reserve(conn, settings, customer_id="cust-1",
                       stable_id="res-memo", amount=40)
        release_credit(conn, settings=settings, tenant_id=TENANT, customer_id="cust-1",
                       stable_id="res-memo")
        # 占用/释放 MUST NOT 产生任何复式分录
        assert query_entries(conn, TENANT) == []


# --------------------------- 守界与跨租户 ---------------------------

def test_unregistered_customer_and_currency_guard(settings):
    with connection(settings) as conn:
        with pytest.raises(ErpError) as ei:
            _reserve(conn, settings, customer_id="ghost",
                           stable_id="res-x", amount=10)
        assert ei.value.code == "NOT_FOUND"
        with pytest.raises(ErpError) as ei2:
            credit_position(conn, settings=settings, tenant_id=TENANT, customer_id="ghost")
        assert ei2.value.code == "NOT_FOUND"
        # 跨币种额度不在内核口径（避免无出处折算），如实拒绝
        set_credit_limit(conn, tenant_id=TENANT, customer_id="cust-usd",
                         limit_amount=100, currency="USD")
        with pytest.raises(ErpError) as ei3:
            _reserve(conn, settings, customer_id="cust-usd",
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


def test_list_credit_reservations_returns_rows_returned_stamp(settings):
    """只读工具必须如实落行数戳——`rows_returned` 是按行计费的唯一依据。

    2026-11-09 生产实测：工具返回 2 行却报 `rows_returned=0`，因为响应体用
    `reservations` 键而 `stamp()` 只认 `data`，把自己写的戳覆盖成了 0。
    """
    from fd_erp_ledger_mcp.result import stamp

    rows = [{"stable_id": "a"}, {"stable_id": "b"}]
    # 复现旧形状：自定义键 + 自己写 rows_returned
    legacy = stamp({"ok": True, "rows_returned": len(rows), "reservations": rows})
    assert legacy["rows_returned"] == 0, "旧形状确实会被覆盖成 0（这就是那个 bug）"

    # 修后形状：走 paginate，data 是列表，戳应为真实行数
    from fd_erp_ledger_mcp.result import paginate
    fixed = stamp(paginate(rows, offset=0, limit=0))
    assert fixed["rows_returned"] == 2
    assert fixed["data"] == rows
