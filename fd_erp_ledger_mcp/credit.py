"""信用额度（erp-vertical-templates 任务 4.2；失效时点与孤儿识别见
change `close-orphan-credit-reservations`）。

额度口径（design 已裁定）：
    available = limit − ledger 应收余额 − active reservations 之和
  * 应收余额从既有分录确定性重算（通用科目 1300 在途结算款；租户映射应收科目
    留待模板层接入后替换），不存任何可直写余额字段。
  * 占用（reservation）是备忘性质，MUST NOT 写成复式分录；release 只是本表
    状态推进（reserved → released），不是账本事实。
  * 同 stable_id 重推返回首次结果（UNIQUE(tenant_id, stable_id) 幂等兜底）。
  * 币种纪律：额度/占用币种须一致且为本位币；跨币种额度不在本内核口径
    （避免无出处折算），如实拒绝。

失效时点（本 change 新增）：
  * 占用 MUST 带 expires_at，由受理方声明；缺失即拒绝，服务端不填隐藏默认值。
  * 「过期」是派生判定（status='reserved' 且 now() > expires_at），不落库，
    避免一处按列、一处按时间两个口径。
  * 已过期占用的钱**不计入**可用额度，但它仍存在、仍可被处置 —— 过期 ≠ 释放。
  * 存量行 expires_at 为 NULL：按保守口径仍计入占用（宁可额度偏紧，不可击穿）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    """计入可用额度的占用之和。

    - 已过期的占用不计入（钱不再被它锁住）
    - 但 NULL 的存量行仍计入：迁移前写入、尚未处置的占用没有失效时点，
      保守口径下按"仍然有效"处理。宁可额度偏紧，不可击穿。
    """
    row = fetch_one(
        conn,
        "SELECT COALESCE(SUM(amount),0) AS total FROM credit_reservations "
        "WHERE tenant_id=%s AND customer_id=%s AND status='reserved' "
        "AND (expires_at IS NULL OR expires_at > now())",
        (tenant_id, customer_id),
    )
    return Decimal(str(row["total"]))


def _max_reservation_days(settings: Settings) -> int:
    """占用失效时点的上界（天）。

    内核会被兄弟仓以**同进程**形态调用，而它们各自维护自己的 Settings dataclass
    （例如 fd-erp-vertical-api 的进程配置）。因此这里按"有则用、无则取契约默认值"
    读取：缺字段不报错，但默认值就是契约声明的 90，不会静默放宽。
    """
    return int(getattr(settings, "credit_reservation_max_days", 90) or 90)


def _parse_expiry(expires_at: Any, settings: Settings) -> datetime:
    """校验并规范化受理方声明的失效时点。

    缺失即拒绝（不填隐藏默认值）；超过部署上限即拒绝（超长时点等于用口径
    规避对账）；必须是将来时间。
    """
    if expires_at is None or str(expires_at).strip() == "":
        raise ErpError(
            "INVALID_ARGUMENT",
            "占用必须声明 expires_at（失效时点）；未声明不受理",
            detail={"remedy": "受理方按单据类型给出失效时点后重试"},
        )
    if isinstance(expires_at, datetime):
        when = expires_at
    else:
        raw = str(expires_at).strip().replace("Z", "+00:00")
        try:
            when = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ErpError("INVALID_ARGUMENT", f"expires_at 不是合法时间：{expires_at!r}") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if when <= now:
        raise ErpError("INVALID_ARGUMENT", f"expires_at 必须是将来的时间：{when.isoformat()}")
    max_days = _max_reservation_days(settings)
    ceiling = now + timedelta(days=max_days)
    if when > ceiling:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"expires_at 超出配置上限 {max_days} 天",
            detail={"expires_at": when.isoformat(), "max_days": max_days,
                    "remedy": "缩短失效时点，或调整 ERP_CREDIT_RESERVATION_MAX_DAYS"},
        )
    return when


def _derived_status(row: dict[str, Any]) -> str:
    """占用对外状态：reserved / expired / released。

    `expired` 由时间推导，不是库里的枚举值——库里只存 reserved/released。
    """
    if row.get("status") != "reserved":
        return str(row.get("status") or "")
    expires = row.get("expires_at")
    if expires is not None and expires <= datetime.now(timezone.utc):
        return "expired"
    return "reserved"


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
                   source_ref: str | None = None,
                   expires_at: Any = None) -> dict[str, Any]:
    """占用信用额度：额度不足拒绝（CREDIT_EXCEEDED，含额度明细）。

    幂等：同 (tenant, stable_id) 重推返回首次结果，不重复占用。
    失效时点：必须由调用方声明（见 `_parse_expiry`），服务端不填默认值——
    一个默认值会让"这笔额度占多久"这个问题消失，而那正是孤儿产生的根源。
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
    # 校验顺序：先"这个人有没有额度"，再"这笔占用有没有失效时点"——前者更根本，
    # 先报基本问题，别让缺参数的报错盖住未登记客户。
    expiry = _parse_expiry(expires_at, settings)

    existing = fetch_one(
        conn, "SELECT * FROM credit_reservations WHERE tenant_id=%s AND stable_id=%s",
        (tenant_id, stable_id),
    )
    if existing is not None:
        # 同幂等键重推返回首次结果；但"同一键 + 不同失效时点"是另一个请求，
        # 不能就地改写原占用的时点（那等于用重推延长寿命，绕过对账）。
        if existing.get("expires_at") != expiry:
            raise ErpError(
                "INVALID_ARGUMENT",
                "幂等键已存在且失效时点不同；不得就地改写原占用",
                detail={"stable_id": stable_id,
                        "existing_expires_at": str(existing.get("expires_at")),
                        "requested_expires_at": expiry.isoformat()},
            )
        pos = credit_position(conn, settings=settings, tenant_id=tenant_id,
                              customer_id=customer_id)
        return {"ok": True, "available_after": pos["available"], "currency": reg["currency"],
                "reservation_id": existing["id"], "status": _derived_status(existing),
                "expires_at": str(existing.get("expires_at")), "duplicate": True}

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
                "currency, status, source_ref, provenance, expires_at) "
                "VALUES (%s,%s,%s,%s,%s,'reserved',%s,%s::jsonb,%s) RETURNING id",
                (tenant_id, customer_id, stable_id, str(amt), currency, source_ref,
                 '{"kind":"memo","double_entry":false}', expiry),
            )
            reservation_id = cur.fetchone()[0]
    audit(conn, tenant_id, "credit_reserved", actor="system", target=stable_id,
          detail={"customer_id": customer_id, "amount": str(amt),
                  "expires_at": expiry.isoformat()})
    pos = credit_position(conn, settings=settings, tenant_id=tenant_id,
                          customer_id=customer_id)
    return {"ok": True, "available_after": pos["available"], "currency": reg["currency"],
            "reservation_id": reservation_id, "status": "reserved",
            "expires_at": expiry.isoformat(), "duplicate": False}


def release_credit(conn: Any, *, settings: Settings, tenant_id: str, customer_id: str,
                   stable_id: str, released_by: str = "system",
                   reason: str | None = None) -> dict[str, Any]:
    """释放占用（reserved → released；幂等：已释放返回首次结果）。

    可追溯：`released_by` 与 `reason` 一并落库。只保留一个时间戳无法回答
    "谁释放的、凭什么"——那正是处置记录要有发起方的原因。
    """
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
                    "UPDATE credit_reservations SET status='released', released_at=now(), "
                    "released_by=%s, release_reason=%s "
                    "WHERE tenant_id=%s AND id=%s AND status='reserved'",
                    (released_by, reason, tenant_id, row["id"]),
                )
        audit(conn, tenant_id, "credit_released", actor=released_by, target=stable_id,
              detail={"customer_id": customer_id, "amount": str(row["amount"]),
                      "reason": reason or ""})
    pos = credit_position(conn, settings=settings, tenant_id=tenant_id,
                          customer_id=customer_id)
    return {"ok": True, "released": row["status"] == "reserved",
            "already_released": row["status"] != "reserved", "amount": str(row["amount"]),
            "available_after": pos["available"]}


def list_reservations(conn: Any, *, tenant_id: str, customer_id: str | None = None,
                      include_expired: bool = True) -> list[dict[str, Any]]:
    """只读列出占用（供孤儿报表使用）。

    返回派生状态，使调用方能区分 `expired` 与 `released`——两者都"不占额度"，
    但只有后者是被处置过的。本函数不写任何东西。
    """
    sql = ("SELECT id, customer_id, stable_id, amount, currency, status, source_ref, "
           "created_at, expires_at, released_at, released_by, release_reason "
           "FROM credit_reservations WHERE tenant_id=%s")
    params: list[Any] = [tenant_id]
    if customer_id is not None:
        sql += " AND customer_id=%s"
        params.append(customer_id)
    sql += " ORDER BY created_at, id"
    rows = fetch_all(conn, sql, tuple(params))
    out = []
    now = datetime.now(timezone.utc)
    for row in rows:
        derived = _derived_status(row)
        if not include_expired and derived == "expired":
            continue
        out.append({
            "id": row["id"], "customer_id": row["customer_id"], "stable_id": row["stable_id"],
            "amount": str(row["amount"]), "currency": row["currency"],
            "status": derived, "stored_status": row["status"],
            "source_ref": row["source_ref"],
            "created_at": str(row["created_at"]), "expires_at": str(row["expires_at"]),
            "released_at": str(row["released_at"]), "released_by": row["released_by"],
            "release_reason": row["release_reason"],
            "expired": derived == "expired",
            "legacy_no_expiry": row["expires_at"] is None,
            "age_seconds": int((now - row["created_at"].astimezone(timezone.utc)).total_seconds())
            if row["created_at"] else None,
        })
    return out
