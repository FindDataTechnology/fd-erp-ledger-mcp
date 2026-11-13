"""业务日期 + 期间关账（change: add-period-close 任务 1.1-2.3）。

判据（对应 spec `erp-posting-date` / `erp-period-close`）：

1. 业务日期与落账时刻分离：显式业务日期决定期间归属；缺省取账面时区当日；
2. 期间归属与会话时区无关（booked_on 是 DATE，不是 timestamptz）；
3. 已关期间拒落账——**绕应用层直接 SQL 也进不去**（闸在数据库）；
4. 顺序关账、只重开最近一期、重开须理由；
5. 关账快照可复算核对，不一致逐科目点名（用「外部写入一份错快照」证明它会红）。

真库（DB 约束层不可 mock）。
"""
from __future__ import annotations

import datetime
import json

import psycopg
import pytest

from fd_erp_ledger_mcp import periods
from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError, map_psycog_error
from fd_erp_ledger_mcp.ledger import compute_balance, query_entries
from fd_erp_ledger_mcp.proposals import (confirm_proposal, parse_booked_on,
                                        submit_proposal)


def _lines(amount="100"):
    return [
        {"account_code": "5200", "direction": "debit", "amount": amount,
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": amount,
         "currency": "CNY"},
    ]


def _post(s, stable_id, *, booked_on=None, amount="100"):
    with connection(s) as conn:
        sub = submit_proposal(conn, settings=s, tenant_id=s.tenant_id,
                             stable_id=stable_id, lines=_lines(amount),
                             booked_on=booked_on)
        return confirm_proposal(conn, settings=s, tenant_id=s.tenant_id,
                                proposal_id=sub["proposal_id"], confirmed_by="cfo")


def _entries(s, **kw):
    with connection(s) as conn:
        return query_entries(conn, s.tenant_id, **kw)


def _balance(s, code="5200"):
    with connection(s) as conn:
        rows = compute_balance(conn, s.tenant_id, code, settings=s)
    return rows[0]["balance"] if rows else "0"


def _close(s, period, actor="cfo", note=None):
    with connection(s) as conn:
        return periods.close(conn, settings=s, tenant_id=s.tenant_id,
                             period=period, actor=actor, note=note)


def _reopen(s, period, reason="口径修正", actor="cfo"):
    with connection(s) as conn:
        return periods.reopen(conn, settings=s, tenant_id=s.tenant_id,
                              period=period, actor=actor, reason=reason)


# ---- 1) 业务日期 ----

def test_booked_on_decides_the_period_not_the_confirm_time(settings):
    _post(settings, "oct-late", booked_on="2026-10-31")
    rows = _entries(settings, period="2026-10")
    assert [r["stable_id"] for r in rows] == ["oct-late", "oct-late"]
    assert all(r["booked_on"] == "2026-10-31" for r in rows)
    # posted_at 仍是确认时刻——两件事分开存
    assert all(r["posted_at"] != "" for r in rows)
    assert _entries(settings, period="2026-11") == []


def test_default_booked_on_uses_the_book_timezone(settings):
    _post(settings, "today-default")
    expected = settings.today().isoformat()
    assert all(r["booked_on"] == expected for r in _entries(settings))


def test_book_timezone_is_explicit_and_validated(settings):
    from zoneinfo import ZoneInfo
    assert settings.book_timezone == "Asia/Shanghai"
    assert settings.today() == datetime.datetime.now(
        ZoneInfo("Asia/Shanghai")).date()
    with pytest.raises(ValueError):
        Settings(book_timezone="Nowhere/Land")
    # 非法时区在启动期（from_env）就拒，不退回默认值假装没事
    import os
    old = os.environ.get("ERP_BOOK_TIMEZONE")
    os.environ["ERP_BOOK_TIMEZONE"] = "Mars/Olympus"
    try:
        with pytest.raises(ValueError) as ei:
            Settings.from_env()
        assert "ERP_BOOK_TIMEZONE" in str(ei.value)
    finally:
        if old is None:
            os.environ.pop("ERP_BOOK_TIMEZONE", None)
        else:
            os.environ["ERP_BOOK_TIMEZONE"] = old


def test_illegal_booked_on_is_refused(settings):
    for bad in ("2026-13-01", "2026-10-32", "20261031", "2026-10"):
        with pytest.raises(ErpError) as ei:
            parse_booked_on(bad, settings)
        assert ei.value.code == "INVALID_ARGUMENT"
    with pytest.raises(ErpError):
        parse_booked_on(datetime.datetime(2026, 10, 31, 9, 0), settings)
    assert parse_booked_on("2026-10-31", settings) == datetime.date(2026, 10, 31)


def test_period_boundaries_are_half_open(settings):
    _post(settings, "sep-30", booked_on="2026-09-30")
    _post(settings, "oct-01", booked_on="2026-10-01")
    oct_rows = {r["stable_id"] for r in _entries(settings, period="2026-10")}
    sep_rows = {r["stable_id"] for r in _entries(settings, period="2026-09")}
    assert oct_rows == {"oct-01"}
    assert sep_rows == {"sep-30"}


def test_period_filter_rejects_bad_format(settings):
    with pytest.raises(ErpError) as ei:
        _entries(settings, period="2026-1")
    assert ei.value.code == "INVALID_ARGUMENT"


# ---- 2) 冲正落开放期间 ----

def test_reversal_books_today_and_refuses_explicit_date(settings):
    posted = _post(settings, "oct-orig", booked_on="2026-10-05")
    orig_txn = posted["txn_id"]
    with connection(settings) as conn:
        with pytest.raises(ErpError) as ei:
            submit_proposal(conn, settings=settings, tenant_id=settings.tenant_id,
                            stable_id="oct-rev-bad", lines=[],
                            kind="reversal", reversal_of=orig_txn,
                            booked_on="2026-10-05")
        assert ei.value.code == "INVALID_ARGUMENT"
        assert "冲正" in ei.value.message

        sub = submit_proposal(conn, settings=settings, tenant_id=settings.tenant_id,
                              stable_id="oct-rev", lines=[], kind="reversal",
                              reversal_of=orig_txn)
        confirm_proposal(conn, settings=settings, tenant_id=settings.tenant_id,
                         proposal_id=sub["proposal_id"], confirmed_by="cfo")

    rev = [r for r in _entries(settings) if r["is_reversal"]]
    assert rev and all(r["booked_on"] == settings.today().isoformat() for r in rev)
    assert _balance(settings) == "0.000000"


# ---- 3) 关账闸 ----

def test_closing_a_period_blocks_later_postings_into_it(settings):
    _post(settings, "oct-1", booked_on="2026-10-10", amount="100")
    _close(settings, "2026-10")

    with connection(settings) as conn:
        sub = submit_proposal(conn, settings=settings, tenant_id=settings.tenant_id,
                             stable_id="oct-late-2", lines=_lines("50"),
                             booked_on="2026-10-20")
        with pytest.raises(ErpError) as ei:
            confirm_proposal(conn, settings=settings, tenant_id=settings.tenant_id,
                             proposal_id=sub["proposal_id"], confirmed_by="cfo")
    assert ei.value.code == "PERIOD_CLOSED"
    # 零写入：余额没变、提案仍是 pending
    assert _balance(settings) == "100.000000"
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT status FROM proposals WHERE tenant_id=%s AND stable_id='oct-late-2'",
            (settings.tenant_id,)).fetchone()
    assert row[0] == "pending"


def test_db_gate_also_blocks_raw_sql(settings):
    """绕应用层：直接 INSERT 也必须被触发器拒绝，且翻译成 PERIOD_CLOSED 而不是「库抖了」。"""
    _post(settings, "oct-2", booked_on="2026-10-10")
    _close(settings, "2026-10")

    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        with pytest.raises(psycopg.Error) as ei:
            conn.execute(
                "INSERT INTO journal_entries(tenant_id, txn_id, line_no, account_code,"
                " direction, amount, currency, amount_base, stable_id, booked_on)"
                " VALUES (%s,'raw-txn',1,'5200','debit',1,'CNY',1,'raw-stable','2026-10-31')",
                (settings.tenant_id,))
        assert "ERP_PERIOD_CLOSED" in str(ei.value)
        assert map_psycog_error(ei.value).code == "PERIOD_CLOSED"
    finally:
        conn.close()


def test_open_period_still_accepts_postings(settings):
    _post(settings, "oct-3", booked_on="2026-10-10")
    _close(settings, "2026-10")
    _post(settings, "nov-1", booked_on="2026-11-02")
    assert _balance(settings) == "200.000000"
    assert {r["booked_on"] for r in _entries(settings, period="2026-11")} == {"2026-11-02"}


# ---- 4) 顺序关账 / 重开规则 ----

def test_close_is_sequential(settings):
    _post(settings, "sep-1", booked_on="2026-09-15")
    _post(settings, "oct-4", booked_on="2026-10-15")
    with pytest.raises(ErpError) as ei:
        _close(settings, "2026-10")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "2026-09" in ei.value.message
    assert ei.value.detail["unclosed"] == ["2026-09"]

    _close(settings, "2026-09")
    out = _close(settings, "2026-10")
    assert out["closed"] is True


def test_close_is_idempotent(settings):
    _post(settings, "oct-5", booked_on="2026-10-15")
    first = _close(settings, "2026-10", note="月结")
    again = _close(settings, "2026-10")
    assert first["alreadyClosed"] is False
    assert again["alreadyClosed"] is True
    with connection(settings) as conn:
        n = conn.execute(
            "SELECT count(*) FROM accounting_periods WHERE tenant_id=%s AND period='2026-10'",
            (settings.tenant_id,)).fetchone()[0]
    assert n == 1


def test_close_requires_actor_and_refuses_future(settings):
    with pytest.raises(ErpError):
        _close(settings, "2026-10", actor="  ")
    with pytest.raises(ErpError) as ei:
        _close(settings, settings.period_of(settings.today() + datetime.timedelta(days=62)))
    assert "未来" in ei.value.message


def test_reopen_only_latest_and_needs_reason(settings):
    _post(settings, "sep-2", booked_on="2026-09-15")
    _post(settings, "oct-6", booked_on="2026-10-15")
    _close(settings, "2026-09")
    _close(settings, "2026-10")

    with pytest.raises(ErpError) as ei:
        _reopen(settings, "2026-09")
    assert ei.value.detail["latestClosed"] == "2026-10"

    with pytest.raises(ErpError) as ei:
        _reopen(settings, "2026-10", reason="   ")
    assert "理由" in ei.value.message

    out = _reopen(settings, "2026-10")
    assert out["closed"] is False
    # 重开后可再落账，且再关会追加第三条记录（不覆盖历史）
    _post(settings, "oct-7", booked_on="2026-10-20")
    _close(settings, "2026-10")
    with connection(settings) as conn:
        actions = [r[0] for r in conn.execute(
            "SELECT action FROM accounting_periods WHERE tenant_id=%s AND period='2026-10'"
            " ORDER BY id", (settings.tenant_id,)).fetchall()]
    assert actions == ["close", "reopen", "close"]


def test_period_records_are_append_only(settings):
    _post(settings, "oct-8", booked_on="2026-10-15")
    _close(settings, "2026-10")
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        with pytest.raises(psycopg.Error) as ei:
            conn.execute("UPDATE accounting_periods SET action='reopen' WHERE period='2026-10'")
        assert "ERP_APPEND_ONLY" in str(ei.value)
        with pytest.raises(psycopg.Error):
            conn.execute("DELETE FROM accounting_periods WHERE period='2026-10'")
    finally:
        conn.close()


# ---- 5) 快照复算 ----

def test_report_verifies_the_snapshot(settings):
    _post(settings, "oct-9", booked_on="2026-10-15", amount="100")
    _post(settings, "oct-10", booked_on="2026-10-16", amount="50")
    _close(settings, "2026-10")
    with connection(settings) as conn:
        out = periods.report(conn, settings=settings, tenant_id=settings.tenant_id,
                             period="2026-10")
    assert out["verified"] is True
    assert out["diffs"] == []
    assert out["snapshot"]["debitTotal"] == out["snapshot"]["creditTotal"] == "150.000000"
    assert out["snapshot"]["accountCount"] == 2
    assert out["snapshot"]["lines"] == 4


def test_report_catches_a_wrong_snapshot(settings):
    """把一份错快照直接写进台账（外部写入，append-only 允许 INSERT）——复算必须点出来。"""
    _post(settings, "oct-11", booked_on="2026-10-15", amount="100")
    with connection(settings) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO accounting_periods(tenant_id, period, action, actor, snapshot)"
                " VALUES (%s,'2026-10','close','intruder',%s::jsonb)",
                (settings.tenant_id, json.dumps({
                    "period": "2026-10", "from": "2026-10-01", "to": "2026-11-01",
                    "accounts": {"5200": {"debit": "999.000000", "credit": "0.000000",
                                          "net": "999.000000", "lines": 1, "txns": 1}},
                    "debitTotal": "999.000000", "creditTotal": "0.000000",
                    "lines": 1, "accountCount": 1}, ensure_ascii=False)))
        out = periods.report(conn, settings=settings, tenant_id=settings.tenant_id,
                             period="2026-10")
    assert out["verified"] is False
    fields = {(d["account"], d["field"]) for d in out["diffs"]}
    assert ("5200", "debit") in fields
    assert any(d["account"] == "1000" and d["field"] == "existence" for d in out["diffs"])


def test_report_refuses_an_open_period(settings):
    with connection(settings) as conn:
        with pytest.raises(ErpError) as ei:
            periods.report(conn, settings=settings, tenant_id=settings.tenant_id,
                           period="2026-10")
    assert "未关账" in ei.value.message


def test_status_and_list(settings):
    _post(settings, "oct-12", booked_on="2026-10-15")
    _close(settings, "2026-10", note="月结")
    with connection(settings) as conn:
        st = periods.status(conn, settings.tenant_id, "2026-10")
        listed = periods.list_periods(conn, settings.tenant_id)
        open_st = periods.status(conn, settings.tenant_id, "2026-11")
    assert st["closed"] is True and st["note"] == "月结"
    assert st["snapshot"]["lines"] == 2
    assert [p["period"] for p in listed] == ["2026-10"]
    assert open_st["closed"] is False
