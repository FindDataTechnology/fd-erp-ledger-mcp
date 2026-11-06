"""多币种与汇率出处纪律（erp-ledger-kernel 多币种要求）。

fx_facts 从 ecb-exr 观测流做种：每条事实必须带 source；无出处汇率拒绝使用。
DB 层另用 erp_fx_unsourced CHECK 兜底（跨币种分录必须带齐 rate/as_of/source）。
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .config import Settings
from .errors import ErpError


def seed_fx_facts(conn: Any, observations: list[dict[str, Any]],
                  *, settings: Settings) -> dict[str, Any]:
    """把 ecb-exr 形状的观测流落入 fx_facts。

    observation: {quote_currency, rate, as_of, source, [base_currency], [provenance]}
    `source` 缺失/空白一律拒绝（无出处汇率拒绝）；重复 (base,quote,as_of,source) 幂等。
    """
    seeded = 0
    rejected: list[dict[str, Any]] = []
    with conn.transaction():
        with conn.cursor() as cur:
            for i, obs in enumerate(observations):
                source = str(obs.get("source") or "").strip()
                quote = str(obs.get("quote_currency") or "").strip()
                rate = obs.get("rate")
                as_of = obs.get("as_of")
                if not source or not quote or rate in (None, "") or not as_of:
                    rejected.append({"index": i, "reason": "missing source/currency/rate/as_of"})
                    continue
                try:
                    if Decimal(str(rate)) <= 0:
                        rejected.append({"index": i, "reason": "rate must be > 0"})
                        continue
                except Exception:
                    rejected.append({"index": i, "reason": "rate not numeric"})
                    continue
                cur.execute(
                    "INSERT INTO fx_facts(base_currency, quote_currency, rate, as_of, source, provenance) "
                    "VALUES (%s,%s,%s,%s,%s,%s::jsonb) "
                    "ON CONFLICT (base_currency, quote_currency, as_of, source) DO NOTHING",
                    (str(obs.get("base_currency") or settings.base_currency),
                     quote, str(rate), as_of, source,
                     json.dumps(obs.get("provenance") or {}, ensure_ascii=False)),
                )
                seeded += cur.rowcount
    return {"seeded": seeded, "rejected": rejected,
            "requires_source": True}  # 如实口径：无出处不入库


def resolve_rate(conn: Any, quote_currency: str, as_of: str, source: str,
                 *, settings: Settings) -> Decimal | None:
    """按出处查询已登记的汇率事实；无出处或无事实返回 None（调用方拒绝）。"""
    if not source or not str(source).strip():
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rate FROM fx_facts WHERE base_currency=%s AND quote_currency=%s "
            "AND as_of=%s AND source=%s",
            (settings.base_currency, quote_currency, as_of, source),
        )
        row = cur.fetchone()
    return Decimal(str(row[0])) if row else None


def validate_line_fx(line: dict[str, Any], *, settings: Settings) -> None:
    """落账前校验单行汇率出处；DB CHECK 是第二道闸。"""
    currency = str(line.get("currency") or settings.base_currency)
    if currency == settings.base_currency:
        return
    rate = line.get("fx_rate")
    as_of = line.get("fx_as_of")
    source = str(line.get("fx_source") or "").strip()
    if not rate or not as_of or not source:
        raise ErpError(
            "FX_UNSOURCED",
            f"跨币种分录 {currency} 缺少可溯汇率（rate/as_of/source）",
            detail={"currency": currency, "has_rate": bool(rate),
                    "has_as_of": bool(as_of), "has_source": bool(source)},
        )
