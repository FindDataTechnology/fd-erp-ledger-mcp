"""复式记账内核（erp-ledger-kernel 复式完整性 / 只进不改存证 / 多币种）。

- 分录是唯一事实源；余额一律由分录确定性重算（D3），从不存可直写余额字段。
- 借贷必平由 DB 约束触发器在 COMMIT 时强制，应用层不预先放平（场景要求
  「不平分录被确认 → DB 拒绝」）。
- 更正只走冲正：生成反向分录 + reversal_of 关联，原分录原样保留。
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from .config import Settings
from .db import fetch_all
from .errors import ErpError, map_psycog_error
from .fx import resolve_rate, validate_line_fx


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ErpError("INVALID_ARGUMENT", f"金额不是数字：{value!r}") from exc


def _normalize_lines(raw_lines: list[dict[str, Any]], *, settings: Settings,
                     conn: Any) -> list[dict[str, Any]]:
    """校验并规范化提案分录行；计算本位币折算；拒绝无出处汇率。"""
    if not isinstance(raw_lines, list) or len(raw_lines) < 2:
        raise ErpError("INVALID_ARGUMENT", "复式分录至少需要两行（借 + 贷）",
                       detail={"lines": len(raw_lines) if isinstance(raw_lines, list) else None})
    out: list[dict[str, Any]] = []
    for i, line in enumerate(raw_lines):
        direction = str(line.get("direction") or "").lower()
        if direction not in ("debit", "credit"):
            raise ErpError("INVALID_ARGUMENT", f"第 {i} 行 direction 非法（须 debit/credit）")
        account = str(line.get("account_code") or "").strip()
        if not account:
            raise ErpError("INVALID_ARGUMENT", f"第 {i} 行缺 account_code")
        amount = _dec(line.get("amount"))
        if amount <= 0:
            raise ErpError("INVALID_ARGUMENT", f"第 {i} 行 amount 须 > 0")
        currency = str(line.get("currency") or settings.base_currency)
        validate_line_fx(line, settings=settings)
        if currency == settings.base_currency:
            amount_base = amount
            fx_rate = fx_as_of = fx_source = None
        else:
            rate = _dec(line.get("fx_rate"))
            as_of = str(line.get("fx_as_of"))
            source = str(line.get("fx_source"))
            # 出处纪律：汇率事实必须在 fx_facts 有登记（来自 ecb-exr 观测流）
            grounded = resolve_rate(conn, currency, as_of, source, settings=settings)
            if grounded is None or grounded != rate:
                raise ErpError(
                    "FX_UNSOURCED",
                    f"汇率无出处或与 fx_facts 不符：{currency} {as_of} {source}",
                    detail={"currency": currency, "as_of": as_of, "source": source,
                            "declared_rate": str(rate),
                            "registered_rate": str(grounded) if grounded is not None else None},
                )
            amount_base = (amount * rate).quantize(Decimal("0.000001"))
            fx_rate, fx_as_of, fx_source = str(rate), as_of, source
        out.append({
            "line_no": i + 1,
            "account_code": account,
            "direction": direction,
            "amount": str(amount),
            "currency": currency,
            "amount_base": str(amount_base),
            "fx_rate": fx_rate,
            "fx_as_of": fx_as_of,
            "fx_source": fx_source,
        })
    return out


def build_reversal_lines(conn: Any, tenant_id: str, original_txn: str) -> list[dict[str, Any]]:
    """按原分录生成红字/反向分录（借↔贷互换），保留原币与折算口径。"""
    rows = fetch_all(
        conn,
        "SELECT account_code, direction, amount, currency, amount_base, "
        "fx_rate, fx_as_of, fx_source, dimensions FROM journal_entries "
        "WHERE tenant_id=%s AND txn_id=%s ORDER BY line_no",
        (tenant_id, original_txn),
    )
    if not rows:
        raise ErpError("NOT_FOUND", f"原分录不存在：{original_txn}",
                       detail={"txn_id": original_txn})
    lines: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        lines.append({
            "line_no": i + 1,
            "account_code": r["account_code"],
            "direction": "credit" if r["direction"] == "debit" else "debit",
            "amount": str(r["amount"]),
            "currency": r["currency"],
            "amount_base": str(r["amount_base"]),
            "fx_rate": str(r["fx_rate"]) if r["fx_rate"] is not None else None,
            "fx_as_of": str(r["fx_as_of"]) if r["fx_as_of"] is not None else None,
            "fx_source": r["fx_source"],
            # 冲正必须落在同一维度：否则红字跑到别的维度，该维度净额虚高、原维度虚低，
            # 而账本借贷仍然平——这种错最难看出来的就是它（design D3）。
            "dimensions": r.get("dimensions") or {},
        })
    return lines


def _line_dimensions(line: dict[str, Any],
                     proposal_dimensions: dict[str, str] | None) -> dict[str, str]:
    """行级维度优先，其次提案级。

    冲正分录由原分录派生、带着原维度（design D3）；普通提案的维度只在提案上，
    由这里下发给每一行（design D2）。两处都为空即「未标注」。
    """
    line_dims = line.get("dimensions")
    if isinstance(line_dims, dict) and line_dims:
        return line_dims
    return proposal_dimensions or {}


def post_lines(conn: Any, *, settings: Settings, tenant_id: str, stable_id: str,
               lines: list[dict[str, Any]], source_ref: str | None,
               confirmed_by: str, is_reversal: bool = False,
               reversal_of: str | None = None,
               dimensions: dict[str, str] | None = None) -> dict[str, Any]:
    """把已确认提案的分录行落账。返回 txn_id 与行数。

    借贷必平由 DB 延迟约束触发器在 COMMIT 时判定：不平则整笔回滚、
    异常上抛为 UNBALANCED（proposal 保持 pending，账本零变化）。
    """
    txn_id = str(uuid.uuid4())
    provenance = json.dumps({
        "stable_id": stable_id,
        "source_ref": source_ref,
        "confirmed_by": confirmed_by,
        "base_currency": settings.base_currency,
    }, ensure_ascii=False)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                for line in lines:
                    cur.execute(
                        "INSERT INTO journal_entries("
                        "tenant_id, txn_id, line_no, account_code, direction, amount, currency, "
                        "amount_base, fx_rate, fx_as_of, fx_source, stable_id, source_ref, "
                        "is_reversal, reversal_of, provenance, dimensions) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) "
                        "ON CONFLICT (tenant_id, stable_id, line_no) DO NOTHING",
                        (tenant_id, txn_id, line["line_no"], line["account_code"],
                         line["direction"], line["amount"], line["currency"],
                         line["amount_base"], line["fx_rate"], line["fx_as_of"],
                         line["fx_source"], stable_id, source_ref,
                         is_reversal, reversal_of, provenance,
                         json.dumps(_line_dimensions(line, dimensions),
                                    ensure_ascii=False)),
                    )
    except Exception as exc:
        # 借贷必平是 DEFERRABLE 约束：在事务 COMMIT 时触发，异常从 with 块边界
        # 抛出。此处整笔回滚（proposal 保持 pending、账本零变化），并翻译为封闭码。
        raise map_psycog_error(exc) from exc
    return {"txn_id": txn_id, "lines": len(lines), "stable_id": stable_id,
            "is_reversal": is_reversal, "reversal_of": reversal_of}


def compute_balance(conn: Any, tenant_id: str, account_code: str | None,
                    *, settings: Settings) -> list[dict[str, Any]]:
    """余额 = 分录的确定性查询（SUM(debit)-SUM(credit)，本位币口径）。"""
    sql = (
        "SELECT account_code, currency, "
        "SUM(CASE WHEN direction='debit' THEN amount_base ELSE 0 END) AS debit_total, "
        "SUM(CASE WHEN direction='credit' THEN amount_base ELSE 0 END) AS credit_total, "
        "SUM(CASE WHEN direction='debit' THEN amount_base ELSE -amount_base END) AS balance, "
        "COUNT(*) AS entry_count "
        "FROM journal_entries WHERE tenant_id=%s "
    )
    params: list[Any] = [tenant_id]
    if account_code:
        sql += "AND account_code=%s "
        params.append(account_code)
    sql += "GROUP BY account_code, currency ORDER BY account_code, currency"
    rows = fetch_all(conn, sql, tuple(params))
    for r in rows:
        for k in ("debit_total", "credit_total", "balance"):
            r[k] = str(r[k])
        r["currency_of_balance"] = settings.base_currency
    return rows


def query_entries(conn: Any, tenant_id: str, *, account_code: str | None = None,
                  txn_id: str | None = None,
                  dimensions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT txn_id, line_no, account_code, direction, amount, currency, "
           "amount_base, fx_rate, fx_as_of, fx_source, stable_id, source_ref, "
           "is_reversal, reversal_of, posted_at, provenance, dimensions "
           "FROM journal_entries WHERE tenant_id=%s ")
    params: list[Any] = [tenant_id]
    if account_code:
        sql += "AND account_code=%s "
        params.append(account_code)
    if txn_id:
        sql += "AND txn_id=%s "
        params.append(txn_id)
    if dimensions:
        # 包含语义：给几个键就是「全部命中」（design D4）。
        sql += "AND dimensions @> %s::jsonb "
        params.append(json.dumps(dimensions, ensure_ascii=False))
    sql += "ORDER BY posted_at, txn_id, line_no"
    rows = fetch_all(conn, sql, tuple(params))
    for r in rows:
        for k in ("amount", "amount_base", "fx_rate"):
            if r[k] is not None:
                r[k] = str(r[k])
        for k in ("fx_as_of", "posted_at"):
            if r[k] is not None:
                r[k] = str(r[k])
    return rows
