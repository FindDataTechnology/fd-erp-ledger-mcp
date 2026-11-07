"""信用额度（erp-vertical-templates 任务 4.2）。

额度口径（design 已裁定）：
    available = limit − ledger 应收余额 − active reservations 之和
  * 应收余额从既有分录确定性重算（通用科目 1300 在途结算款；租户映射应收科目
    留待模板层接入后替换），不存任何可直写余额字段。
  * 占用（reservation）是备忘性质，MUST NOT 写成复式分录；release 只是本表
    状态推进（reserved → released），不是账本事实。
  * 同 stable_id 重推返回首次结果（UNIQUE(tenant_id, stable_id) 幂等兜底）。
  * 币种纪律：额度/占用币种须一致且为本位币；跨币种额度不在本内核口径
    （避免无出处折算），如实拒绝。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .config import Settings
from .db import fetch_all, fetch_one
from .errors import ErpError
from .tenancy import audit

# 应收科目口径：通用科目 1300 在途结算款（渠道/平台已确认未到账的在途资金）。
# 租户映射的应收科目（如 1100）归模板层科目映射 pack，接入后在此替换。
AR_ACCOUNT = "1300"


def _dec(value: Any, *, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ErpError("INVALID_ARGUMENT", f"{what} 不是数字：{value!r}") from exc


def set_credit_limit(conn: Any, *, tenant_id: str, customer_id: str, limit_amount: Any,
                     currency: str = "CNY", source_ref: str | None = None) -> dict[str, Any]:
    """登记/调增客户信用额度上限（运维动作；额度事实带出处）。

    幂等 upsert：同 (tenant, customer) 覆盖更新 limit（历史经 audit 留痕）。
    本函数是模块级 API（供 CLI/运维），不进 MCP 工具面——工具面按契约固定为
    reserve/release/query 三件。
    """
    customer_id = str(customer_id or "").strip()
    if not customer_id:
        raise ErpError("INVALID_ARGUMENT", "customer_id 不能为空")
    limit = _dec(limit_amount, what="limit_amount")
    if limit < 0:
        raise ErpError("INVALID_ARGUMENT", "limit_amount 不得为负")
    currency = str(currency or "").strip()
    if not currency:
        raise ErpError("INVALID_ARGUMENT", "currency 不能为空")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO credit_registry(tenant_id, customer_id, limit_amount, currency, "
                "source_ref) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, customer_id) DO UPDATE SET "
                "limit_amount = EXCLUDED.limit_amount, currency = EXCLUDED.currency, "
                "source_ref = EXCLUDED.source_ref, updated_at = now()",
                (tenant_id, customer_id, str(limit), currency, source_ref),
            )
    audit(conn, tenant_id, "credit_limit_set", actor="ops", target=customer_id,
          detail={"limit_amount": str(limit), "currency": currency, "source_ref": source_ref})
    return {"customer_id": customer_id, "limit_amount": str(limit), "currency": currency}


def ar_balance(conn: Any, tenant_id: str) -> Decimal:
    """应收余额 = 分录确定性重算（SUM(debit) − SUM(credit)，科目 1300，本位币口径）。"""
    row = fetch_one(
        conn,
        "SELECT COALESCE(SUM(CASE WHEN direction='debit' THEN amount_base "
        "ELSE -amount_base END),0) AS bal FROM journal_entries "
        "WHERE tenant_id=%s AND account_code=%s",
        (tenant_id, AR_ACCOUNT),
    )
    return Decimal(str(row["bal"]))


def reserved_total(conn: Any, tenant_id: str, customer_id: str) -> Decimal:
    """active reservations 之和（status='reserved'）。"""
    row = fetch_one(
        conn,
        "SELECT COALESCE(SUM(amount),0) AS total FROM credit_reservations "
        "WHERE tenant_id=%s AND customer_id=%s AND status='reserved'",
        (tenant_id, customer_id),
    )
    return Decimal(str(row["total"]))


def _registry(conn: Any, tenant_id: str, customer_id: str) -> dict[str, Any]:
    row = fetch_one(
        conn, "SELECT * FROM credit_registry WHERE tenant_id=%s AND customer_id=%s",
        (tenant_id, customer_id),
    )
    if row is None:
        raise ErpError("NOT_FOUND", "该客户未登记信用额度",
                       detail={"customer_id": customer_id})
    return row


def _base_currency_guard(settings: Settings, currency: str) -> None:
    if currency != settings.base_currency:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"信用额度币种须为本位币 {settings.base_currency}（跨币种额度不在内核口径，"
            "避免无出处折算）",
            detail={"currency": currency, "base_currency": settings.base_currency},
        )


def credit_position(conn: Any, *, settings: Settings, tenant_id: str,
                    customer_id: str) -> dict[str, Any]:
    """额度位：limit / ar_balance / reserved / available（全部确定性推导）。"""
    reg = _registry(conn, tenant_id, customer_id)
    _base_currency_guard(settings, reg["currency"])
    limit = Decimal(str(reg["limit_amount"]))
    ar = ar_balance(conn, tenant_id)
    reserved = reserved_total(conn, tenant_id, customer_id)
    available = (limit - ar - reserved).quantize(Decimal("0.000001"))
    return {
        "customer_id": customer_id,
        "limit": str(limit.quantize(Decimal("0.000001"))),
        "ar_balance": str(ar.quantize(Decimal("0.000001"))),
        "reserved": str(reserved.quantize(Decimal("0.000001"))),
        "available": str(available),
        "currency": reg["currency"],
        "ar_account": AR_ACCOUNT,
    }


def reserve_credit(conn: Any, *, settings: Settings, tenant_id: str, customer_id: str,
                   stable_id: str, amount: Any, currency: str = "CNY",
                   source_ref: str | None = None) -> dict[str, Any]:
    """占用信用额度：额度不足拒绝（CREDIT_EXCEEDED，含额度明细）。

    幂等：同 (tenant, stable_id) 重推返回首次结果，不重复占用。
    """
    customer_id = str(customer_id or "").strip()
    stable_id = str(stable_id or "").strip()
    if not customer_id or not stable_id:
        raise ErpError("INVALID_ARGUMENT", "customer_id / stable_id 不能为空")
    amt = _dec(amount, what="amount")
    if amt <= 0:
        raise ErpError("INVALID_ARGUMENT", "amount 须 > 0")
    reg = _registry(conn, tenant_id, customer_id)
    _base_currency_guard(settings, reg["currency"])
    if currency != reg["currency"]:
        raise ErpError("INVALID_ARGUMENT",
                       f"占用币种 {currency} 与额度币种 {reg['currency']} 不一致")

    existing = fetch_one(
        conn, "SELECT * FROM credit_reservations WHERE tenant_id=%s AND stable_id=%s",
        (tenant_id, stable_id),
    )
    if existing is not None:
        pos = credit_position(conn, settings=settings, tenant_id=tenant_id,
                              customer_id=customer_id)
        return {"ok": True, "available_after": pos["available"], "currency": reg["currency"],
                "reservation_id": existing["id"], "status": existing["status"],
                "duplicate": True}

    # 串行化同客户并发占用：锁登记行后再推导可用额度。
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM credit_registry WHERE tenant_id=%s AND customer_id=%s "
                        "FOR UPDATE", (tenant_id, customer_id))
            limit = Decimal(str(reg["limit_amount"]))
            ar = ar_balance(conn, tenant_id)
            reserved = reserved_total(conn, tenant_id, customer_id)
            available = (limit - ar - reserved).quantize(Decimal("0.000001"))
            if amt > available:
                raise ErpError(
                    "CREDIT_EXCEEDED",
                    f"信用额度不足：可用 {available} < 请求 {amt.quantize(Decimal('0.000001'))}",
                    detail={"customer_id": customer_id,
                            "limit": str(limit.quantize(Decimal("0.000001"))),
                            "ar_balance": str(ar.quantize(Decimal("0.000001"))),
                            "reserved": str(reserved.quantize(Decimal("0.000001"))),
                            "requested": str(amt.quantize(Decimal("0.000001"))),
                            "available": str(available)},
                )
            cur.execute(
                "INSERT INTO credit_reservations(tenant_id, customer_id, stable_id, amount, "
                "currency, status, source_ref, provenance) "
                "VALUES (%s,%s,%s,%s,%s,'reserved',%s,%s::jsonb) RETURNING id",
                (tenant_id, customer_id, stable_id, str(amt), currency, source_ref,
                 '{"kind":"memo","double_entry":false}'),
            )
            reservation_id = cur.fetchone()[0]
    audit(conn, tenant_id, "credit_reserved", actor="system", target=stable_id,
          detail={"customer_id": customer_id, "amount": str(amt)})
    pos = credit_position(conn, settings=settings, tenant_id=tenant_id,
                          customer_id=customer_id)
    return {"ok": True, "available_after": pos["available"], "currency": reg["currency"],
            "reservation_id": reservation_id, "status": "reserved", "duplicate": False}


def release_credit(conn: Any, *, settings: Settings, tenant_id: str, customer_id: str,
                   stable_id: str) -> dict[str, Any]:
    """释放占用（reserved → released；幂等：已释放返回首次结果）。"""
    customer_id = str(customer_id or "").strip()
    stable_id = str(stable_id or "").strip()
    if not customer_id or not stable_id:
        raise ErpError("INVALID_ARGUMENT", "customer_id / stable_id 不能为空")
    row = fetch_one(
        conn, "SELECT * FROM credit_reservations WHERE tenant_id=%s AND customer_id=%s "
              "AND stable_id=%s", (tenant_id, customer_id, stable_id),
    )
    if row is None:
        raise ErpError("NOT_FOUND", "占用记录不存在",
                       detail={"customer_id": customer_id, "stable_id": stable_id})
    if row["status"] == "reserved":
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE credit_reservations SET status='released', released_at=now() "
                    "WHERE tenant_id=%s AND id=%s AND status='reserved'",
                    (tenant_id, row["id"]),
                )
        audit(conn, tenant_id, "credit_released", actor="system", target=stable_id,
              detail={"customer_id": customer_id, "amount": str(row["amount"])})
    pos = credit_position(conn, settings=settings, tenant_id=tenant_id,
                          customer_id=customer_id)
    return {"ok": True, "released": row["status"] == "reserved",
            "already_released": row["status"] != "reserved", "amount": str(row["amount"]),
            "available_after": pos["available"]}
