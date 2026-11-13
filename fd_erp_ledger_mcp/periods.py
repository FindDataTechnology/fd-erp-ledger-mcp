"""期间关账（change: add-period-close）。

账本有了业务日期（`journal_entries.booked_on`）之后，「这个期间报出去了」才能成为事实。
本模块只做四件事：关账、重开、查状态、复算核对。

## 为什么关账必须追加写

关账最怕的不是没关，而是**悄悄改回**。若用可变的 `status` 列，一次 UPDATE 就能把「已关」
改回「未关」，事后谁也说不清当时报的是哪版。所以 `accounting_periods` 只追加
（UPDATE/DELETE 由数据库拒绝），当前状态 = 该期间最新一条 close/reopen。

## 为什么拦截在数据库

应用层少写一处判断就漏一处（导入车道、脚本、临时 SQL 都在应用层之外）。落账闸是
`journal_entries` 上的 BEFORE INSERT 触发器（见迁移 0014）；本模块不重复判断，
只负责把「已关」这个状态说清楚。

## 快照是「当时报出去的数字」本身

关账那一刻把期间内逐科目借贷与行数写进快照。之后任何时候 `report()` 都能拿复算结果与
快照对比：一致说明没人动过；不一致就逐科目点名差额——而不是回一句「已关账」让人安心。
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .config import Settings, book_period, book_today, resolve_book_timezone
from .db import fetch_all
from .errors import ErpError, map_psycog_error
from .tenancy import audit

Q6 = Decimal("0.000001")
PERIOD_SCHEMA = "fd-erp-ledger-mcp/periods/v1"
PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Q6, rounding=ROUND_HALF_UP))


def _dec(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def normalize_period(value: Any) -> str:
    """期间只认 `YYYY-MM`。格式错就拒——猜「2026-1」是 1 月还是 10 月没有意义。"""
    raw = str(value or "").strip()
    if not PERIOD_RE.match(raw):
        raise ErpError("INVALID_ARGUMENT",
                       f"期间须为 YYYY-MM：{value!r}",
                       detail={"example": "2026-10"})
    return raw


def _bounds(period: str) -> tuple[date, date]:
    """[期初, 次期初) 半开区间——期间归属的唯一算法出处。"""
    year, month = int(period[:4]), int(period[5:])
    start = date(year, month, 1)
    if month == 12:
        return start, date(year + 1, 1, 1)
    return start, date(year, month + 1, 1)


def _history(conn: Any, tenant_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        conn,
        "SELECT id, period, action, actor, note, snapshot, created_at "
        "FROM accounting_periods WHERE tenant_id=%s ORDER BY id",
        (tenant_id,),
    )
    for r in rows:
        r["created_at"] = str(r["created_at"])
        if isinstance(r.get("snapshot"), str):
            r["snapshot"] = json.loads(r["snapshot"])
    return rows


def _state(conn: Any, tenant_id: str) -> dict[str, dict[str, Any]]:
    """期间 → 最新一条记录（追加式回放：当前状态只由最后一条决定）。"""
    state: dict[str, dict[str, Any]] = {}
    for row in _history(conn, tenant_id):
        state[row["period"]] = row
    return state


def _closed_periods(conn: Any, tenant_id: str) -> list[str]:
    return sorted(p for p, row in _state(conn, tenant_id).items() if row["action"] == "close")


def _snapshot(conn: Any, tenant_id: str, period: str) -> dict[str, Any]:
    """期间内逐科目借贷与行数 + 借贷合计。关账写进快照，report 拿去核对。"""
    start, end = _bounds(period)
    rows = fetch_all(
        conn,
        "SELECT account_code, "
        " COALESCE(SUM(amount_base) FILTER (WHERE direction='debit'),0) AS debit, "
        " COALESCE(SUM(amount_base) FILTER (WHERE direction='credit'),0) AS credit, "
        " COUNT(*) AS lines, COUNT(DISTINCT txn_id) AS txns "
        "FROM journal_entries "
        "WHERE tenant_id=%s AND booked_on >= %s AND booked_on < %s "
        "GROUP BY account_code ORDER BY account_code",
        (tenant_id, start, end),
    )
    accounts: dict[str, dict[str, Any]] = {}
    debit_total = Decimal(0)
    credit_total = Decimal(0)
    lines = 0
    for r in rows:
        debit, credit = _dec(r["debit"]), _dec(r["credit"])
        accounts[r["account_code"]] = {
            "debit": _fmt(debit), "credit": _fmt(credit),
            "net": _fmt(debit - credit), "lines": int(r["lines"]),
            "txns": int(r["txns"]),
        }
        debit_total += debit
        credit_total += credit
        lines += int(r["lines"])
    return {
        "period": period,
        "from": start.isoformat(),
        "to": end.isoformat(),          # 次期初（半开区间上界，不含）
        "accounts": accounts,
        "debitTotal": _fmt(debit_total),
        "creditTotal": _fmt(credit_total),
        "lines": lines,
        "accountCount": len(accounts),
    }


def status(conn: Any, tenant_id: str, period: str) -> dict[str, Any]:
    """某期间当前是否关账 + 最近一次动作的归属与快照摘要。"""
    period = normalize_period(period)
    row = _state(conn, tenant_id).get(period)
    if row is None:
        return {"schema": PERIOD_SCHEMA, "period": period, "closed": False}
    snap = row.get("snapshot") or {}
    return {
        "schema": PERIOD_SCHEMA,
        "period": period,
        "closed": row["action"] == "close",
        "lastAction": row["action"],
        "actor": row["actor"],
        "note": row["note"],
        "decidedAt": row["created_at"],
        "snapshot": {
            "lines": snap.get("lines", 0),
            "accountCount": snap.get("accountCount", 0),
            "debitTotal": snap.get("debitTotal", "0.000000"),
            "creditTotal": snap.get("creditTotal", "0.000000"),
        } if row["action"] == "close" else None,
    }


def list_periods(conn: Any, tenant_id: str) -> list[dict[str, Any]]:
    """所有有记录的期间（关过或重开过）+ 当前状态，按期间升序。"""
    state = _state(conn, tenant_id)
    return [{"period": p, "closed": state[p]["action"] == "close",
             "actor": state[p]["actor"], "decidedAt": state[p]["created_at"]}
            for p in sorted(state)]


def close(conn: Any, *, settings: Settings, tenant_id: str, period: str,
          actor: str, note: str | None = None) -> dict[str, Any]:
    """关账：顺序关账 + 写快照。已关账的期间重复调用是幂等的（不新增记录）。"""
    period = normalize_period(period)
    actor = str(actor or "").strip()
    if not actor:
        raise ErpError("INVALID_ARGUMENT", "关账须记录操作者（actor）")

    existing = _state(conn, tenant_id).get(period)
    if existing is not None and existing["action"] == "close":
        return {**status(conn, tenant_id, period), "alreadyClosed": True,
                "idempotent_hit": True}

    today = book_today(settings)
    today_period = book_period(settings, today)
    if period > today_period:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"不能关未来期间：{period}（账面今天 {today.isoformat()} 属 {today_period}）",
            detail={"period": period, "todayPeriod": today_period})

    # 顺序关账：早于本期且有分录的期间必须先关。先关后期再回头改前期，
    # 会让已报出的后一期数字失去依据——而那种错在报表上看不出来。
    start, _end = _bounds(period)
    earlier_rows = fetch_all(
        conn,
        "SELECT DISTINCT to_char(booked_on,'YYYY-MM') AS p FROM journal_entries "
        "WHERE tenant_id=%s AND booked_on < %s ORDER BY p",
        (tenant_id, start),
    )
    closed = set(_closed_periods(conn, tenant_id))
    unclosed = [r["p"] for r in earlier_rows if r["p"] not in closed]
    if unclosed:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"不能越过未关期间：{period} 之前还有 {unclosed[0]} 等 {len(unclosed)} 期未关账"
            "（须按期间顺序关账）",
            detail={"period": period, "unclosed": unclosed},
        )

    snap = _snapshot(conn, tenant_id, period)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO accounting_periods"
                    "(tenant_id, period, action, actor, note, snapshot) "
                    "VALUES (%s,%s,'close',%s,%s,%s::jsonb)",
                    (tenant_id, period, actor, note,
                     json.dumps(snap, ensure_ascii=False)),
                )
    except Exception as exc:  # 触发器/连接层异常
        raise map_psycog_error(exc) from exc

    audit(conn, tenant_id, "period_closed", actor=actor, target=period,
          detail={"lines": snap["lines"], "accounts": snap["accountCount"],
                  "debitTotal": snap["debitTotal"], "creditTotal": snap["creditTotal"]})
    return {**status(conn, tenant_id, period), "alreadyClosed": False,
            "idempotent_hit": False}


def reopen(conn: Any, *, settings: Settings, tenant_id: str, period: str,
           actor: str, reason: str) -> dict[str, Any]:
    """重开：只认最近一个已关期间，且必须给理由（谁、为什么把报出去的数字改回来）。"""
    period = normalize_period(period)
    actor = str(actor or "").strip()
    if not actor:
        raise ErpError("INVALID_ARGUMENT", "重开须记录操作者（actor）")
    reason = str(reason or "").strip()
    if not reason:
        raise ErpError("INVALID_ARGUMENT", "重开须给出理由（reason）",
                       detail={"why": "重开会改动已报出的数字，理由是这个动作的审计依据"})

    closed = _closed_periods(conn, tenant_id)
    if period not in closed:
        raise ErpError("INVALID_ARGUMENT", f"期间未关账，无需重开：{period}")
    latest = closed[-1]
    if period != latest:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"只能重开最近一个已关期间：最近一期是 {latest}，{period} 之后已有期间关账",
            detail={"period": period, "latestClosed": latest},
        )

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO accounting_periods(tenant_id, period, action, actor, note) "
                    "VALUES (%s,%s,'reopen',%s,%s)",
                    (tenant_id, period, actor, reason),
                )
    except Exception as exc:
        raise map_psycog_error(exc) from exc

    audit(conn, tenant_id, "period_reopened", actor=actor, target=period,
          detail={"reason": reason})
    return status(conn, tenant_id, period)


def _diff(snapshot: dict[str, Any], recomputed: dict[str, Any]) -> list[dict[str, Any]]:
    """逐科目对比快照与复算：只报差异，不报「差不多」。"""
    diffs: list[dict[str, Any]] = []
    before = snapshot.get("accounts") or {}
    now = recomputed.get("accounts") or {}
    for code in sorted(set(before) | set(now)):
        b, n = before.get(code), now.get(code)
        if b is None or n is None:
            diffs.append({"account": code, "field": "existence",
                          "snapshot": "有" if b else "无", "now": "有" if n else "无"})
            continue
        for field in ("debit", "credit", "lines"):
            if _dec(b.get(field)) != _dec(n.get(field)):
                diffs.append({"account": code, "field": field,
                              "snapshot": str(b.get(field)), "now": str(n.get(field)),
                              "delta": _fmt(_dec(n.get(field)) - _dec(b.get(field)))})
    for field in ("debitTotal", "creditTotal", "lines"):
        if _dec(snapshot.get(field)) != _dec(recomputed.get(field)):
            diffs.append({"account": "*", "field": field,
                          "snapshot": str(snapshot.get(field)),
                          "now": str(recomputed.get(field))})
    return diffs


def report(conn: Any, *, settings: Settings, tenant_id: str, period: str) -> dict[str, Any]:
    """关账快照 vs 当前复算。不一致就逐科目点名差额——不静默返回「已关账」。"""
    period = normalize_period(period)
    row = _state(conn, tenant_id).get(period)
    if row is None or row["action"] != "close":
        raise ErpError("INVALID_ARGUMENT", f"期间未关账：{period}",
                       detail={"period": period})
    snapshot = row.get("snapshot") or {}
    recomputed = _snapshot(conn, tenant_id, period)
    diffs = _diff(snapshot, recomputed)
    return {
        "schema": PERIOD_SCHEMA,
        "period": period,
        "closedAt": row["created_at"],
        "closedBy": row["actor"],
        "bookTimezone": resolve_book_timezone(settings),
        "verified": not diffs,
        "snapshot": snapshot,
        "recomputed": recomputed,
        "diffs": diffs,
    }
