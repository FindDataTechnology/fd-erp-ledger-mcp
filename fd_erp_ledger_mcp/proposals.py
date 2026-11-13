"""确认车道（erp-ledger-kernel agent 只写 proposal）。

- submit_proposal 只创建 pending 提案，绝不改变账本状态。
- confirm_proposal 是拟稿车道的落账入口（导入车道事实不经确认）；对齐柏讯 propose/confirm 契约的
  fleet identity turn 车道（fleet=/post=/title=/target= 语义字段）。
- 幂等：同 stable_id 重复提交返回首次结果（唯一索引 + SELECT 回读）。
- 拒绝留痕不落账。
"""
from __future__ import annotations

import datetime
import json
from typing import Any

from .config import Settings, book_today
from .db import fetch_all, fetch_one
from .errors import ErpError
from .dimensions import normalize_dimensions
from .ledger import build_reversal_lines, post_lines, _normalize_lines
from .tenancy import audit
from . import observability as obs

# 机器车道白名单（add-supply-chain-vertical 任务 1.x）：计费引擎（submitted_by=
# "billing-engine"）生成的 proposal 科目限 1300 在途结算款 / 4000 营业收入 /
# 费用类白名单（5100 平台及支付费用 / 5200 其他经营费用）；禁 1000/2100/3000/3100。
# 白名单机制天然排除禁用科目（1000 现金、2100 预收、3000/3100 权益均不在集内）。
# 计费引擎 MUST NOT 绕过确认车道：它只经 submit_proposal 出 proposal，
# 落账仍只能走 confirm_proposal 确认门。
MACHINE_LANE_SUBMITTER = "billing-engine"
MACHINE_LANE_ACCOUNTS = frozenset({"1300", "4000", "5100", "5200"})


def parse_booked_on(value: Any, settings: Settings) -> datetime.date:
    """业务日期：显式传入必须合法，缺省取账面时区的今天。

    不接受「按本机时区解释的今天」——那会让同一批单据在不同机器上落进不同期间
    （UTC 23:30 在上海已是次日）。日期字符串只认 `YYYY-MM-DD`。
    """
    if value in (None, ""):
        return book_today(settings)
    if isinstance(value, datetime.datetime):
        raise ErpError("INVALID_ARGUMENT",
                       "业务日期须为 YYYY-MM-DD（date），不接受带时刻的值")
    if isinstance(value, datetime.date):
        return value
    raw = str(value).strip()
    try:
        if len(raw) != 10:
            raise ValueError
        return datetime.date.fromisoformat(raw)
    except ValueError:
        raise ErpError("INVALID_ARGUMENT",
                       f"业务日期不合法：{value!r}（须 YYYY-MM-DD）") from None


def submit_proposal(conn: Any, *, settings: Settings, tenant_id: str,
                    stable_id: str, lines: list[dict[str, Any]],
                    source_ref: str | None = None,
                    kind: str = "journal", reversal_of: str | None = None,
                    lane_fleet: str | None = None, lane_post: str | None = None,
                    lane_title: str | None = None, lane_target: str | None = None,
                    dimensions: Any = None,
                    booked_on: Any = None,
                    submitted_by: str = "agent") -> dict[str, Any]:
    if not stable_id or not str(stable_id).strip():
        raise ErpError("INVALID_ARGUMENT", "stable_id 不能为空")
    if kind not in ("journal", "reversal"):
        raise ErpError("INVALID_ARGUMENT", "kind 须为 journal 或 reversal")
    if kind == "reversal" and not reversal_of:
        raise ErpError("INVALID_ARGUMENT", "冲正提案须带 reversal_of（原 txn_id）")
    if kind == "reversal" and booked_on not in (None, ""):
        # 冲正落在**开放期间**：更正不该回头改已关期间的数，否则关账白关。
        # 静默改写成今天会让调用方以为自己指定成功了——所以直接拒。
        raise ErpError("INVALID_ARGUMENT",
                       "冲正提案不接受业务日期：冲正分录取账面时区当日，落在开放期间")
    # 维度先校验、后查幂等：非法维度不该因为「同 stable_id 已存在」被悄悄放过去。
    dims = normalize_dimensions(dimensions)
    day = parse_booked_on(booked_on, settings)

    # 幂等：已存在同 stable_id 直接返回首次结果，不重复入账、不报错。
    existing = fetch_one(
        conn, "SELECT * FROM proposals WHERE tenant_id=%s AND stable_id=%s",
        (tenant_id, stable_id),
    )
    if existing is not None:
        return _proposal_view(existing, duplicate=True)

    # 冲正：从原分录派生反向分录（忽略传入 lines，保证 append-only 更正不可伪造）
    normalized: list[dict[str, Any]]
    if kind == "reversal":
        normalized = build_reversal_lines(conn, tenant_id, reversal_of)
    else:
        normalized = _normalize_lines(lines, settings=settings, conn=conn)

    # 机器车道闸：凡以 billing-engine 名义提交的 proposal，科目必须落在白名单内。
    # 冲正分录由原分录派生（原行已过闸），天然保持白名单口径。
    if submitted_by == MACHINE_LANE_SUBMITTER:
        bad = sorted({ln["account_code"] for ln in normalized
                      if ln["account_code"] not in MACHINE_LANE_ACCOUNTS})
        if bad:
            raise ErpError(
                "INVALID_ARGUMENT",
                f"机器车道（billing-engine）proposal 含白名单外科目：{', '.join(bad)}",
                detail={"forbidden_accounts": bad,
                        "allowed_accounts": sorted(MACHINE_LANE_ACCOUNTS)},
            )

    payload = json.dumps({"lines": normalized, "kind": kind}, ensure_ascii=False)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO proposals(tenant_id, stable_id, kind, payload, status, "
                "reversal_of, source_ref, lane_fleet, lane_post, lane_title, lane_target, "
                "dimensions, booked_on) "
                "VALUES (%s,%s,%s,%s::jsonb,'pending',%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
                "ON CONFLICT (tenant_id, stable_id) DO NOTHING",
                (tenant_id, stable_id, kind, payload, reversal_of, source_ref,
                 lane_fleet, lane_post, lane_title, lane_target,
                 json.dumps(dims, ensure_ascii=False), day),
            )
    row = fetch_one(
        conn, "SELECT * FROM proposals WHERE tenant_id=%s AND stable_id=%s",
        (tenant_id, stable_id),
    )
    audit(conn, tenant_id, "proposal_submitted", actor=submitted_by,
          target=stable_id, detail={"kind": kind, "reversal_of": reversal_of})
    obs.proposal_submitted(kind, tenant_id=tenant_id)
    return _proposal_view(row)


def _proposal_view(row: dict[str, Any] | None, *, duplicate: bool = False) -> dict[str, Any]:
    if row is None:
        raise ErpError("NOT_FOUND", "proposal 不存在")
    return {
        "proposal_id": row["id"],
        "stable_id": row["stable_id"],
        "status": row["status"],
        "kind": row["kind"],
        "reversal_of": row["reversal_of"],
        "source_ref": row["source_ref"],
        "created_at": str(row["created_at"]),
        "booked_on": str(row["booked_on"]) if row.get("booked_on") else None,
        "dimensions": row.get("dimensions") or {},
        "duplicate": duplicate,
        "idempotent_hit": duplicate,
    }


def list_proposals(conn: Any, tenant_id: str, status: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT id, stable_id, kind, status, reversal_of, source_ref, "
           "lane_fleet, lane_post, lane_title, lane_target, created_at, booked_on, "
           "decided_at, decided_by, decision_note, dimensions "
           "FROM proposals WHERE tenant_id=%s ")
    params: list[Any] = [tenant_id]
    if status:
        sql += "AND status=%s "
        params.append(status)
    sql += "ORDER BY created_at, id"
    rows = fetch_all(conn, sql, tuple(params))
    for r in rows:
        for k in ("created_at", "decided_at", "booked_on"):
            if r.get(k) is not None:
                r[k] = str(r[k])
    return rows


def confirm_proposal(conn: Any, *, settings: Settings, tenant_id: str,
                     proposal_id: int | None = None, stable_id: str | None = None,
                     confirmed_by: str = "human") -> dict[str, Any]:
    if not proposal_id and not stable_id:
        raise ErpError("INVALID_ARGUMENT", "须提供 proposal_id 或 stable_id")
    with conn.cursor() as cur:
        if proposal_id:
            cur.execute("SELECT * FROM proposals WHERE tenant_id=%s AND id=%s",
                        (tenant_id, proposal_id))
        else:
            cur.execute("SELECT * FROM proposals WHERE tenant_id=%s AND stable_id=%s",
                        (tenant_id, stable_id))
        cols = [d.name for d in cur.description]
        raw = cur.fetchone()
    row = dict(zip(cols, raw)) if raw else None
    if row is None:
        raise ErpError("NOT_FOUND", "proposal 不存在",
                       detail={"proposal_id": proposal_id, "stable_id": stable_id})
    if row["status"] == "confirmed":
        # 已确认：返回首次落账结果（幂等），不重复落账
        prior = fetch_one(
            conn, "SELECT txn_id FROM journal_entries WHERE tenant_id=%s AND stable_id=%s LIMIT 1",
            (tenant_id, row["stable_id"]),
        )
        return {"status": "already_confirmed", "proposal_id": row["id"],
                "stable_id": row["stable_id"],
                "txn_id": prior["txn_id"] if prior else None,
                "idempotent_hit": True}
    if row["status"] != "pending":
        raise ErpError("PROPOSAL_STATE", f"proposal 状态为 {row['status']}，不可确认")

    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    lines = payload["lines"]
    posted = post_lines(
        conn, settings=settings, tenant_id=tenant_id,
        stable_id=row["stable_id"], lines=lines,
        source_ref=row["source_ref"], confirmed_by=confirmed_by,
        is_reversal=(row["kind"] == "reversal"),
        reversal_of=row["reversal_of"],
        dimensions=row.get("dimensions") or {},
        booked_on=row.get("booked_on"),
    )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE proposals SET status='confirmed', decided_at=now(), "
                "decided_by=%s WHERE tenant_id=%s AND id=%s AND status='pending'",
                (confirmed_by, tenant_id, row["id"]),
            )
    audit(conn, tenant_id, "proposal_confirmed", actor=confirmed_by,
          target=row["stable_id"], detail={"txn_id": posted["txn_id"],
                                          "kind": row["kind"], "lines": posted["lines"]})
    obs.proposal_confirmed(tenant_id=tenant_id, stable_id=row["stable_id"])
    return {"status": "confirmed", "proposal_id": row["id"],
            "stable_id": row["stable_id"], "txn_id": posted["txn_id"],
            "lines": posted["lines"], "idempotent_hit": False}


def reject_proposal(conn: Any, *, tenant_id: str, proposal_id: int | None = None,
                    stable_id: str | None = None, rejected_by: str = "human",
                    reason: str | None = None) -> dict[str, Any]:
    if not proposal_id and not stable_id:
        raise ErpError("INVALID_ARGUMENT", "须提供 proposal_id 或 stable_id")
    with conn.transaction():
        with conn.cursor() as cur:
            if proposal_id:
                cur.execute(
                    "UPDATE proposals SET status='rejected', decided_at=now(), "
                    "decided_by=%s, decision_note=%s "
                    "WHERE tenant_id=%s AND id=%s AND status='pending' RETURNING id, stable_id",
                    (rejected_by, reason, tenant_id, proposal_id),
                )
            else:
                cur.execute(
                    "UPDATE proposals SET status='rejected', decided_at=now(), "
                    "decided_by=%s, decision_note=%s "
                    "WHERE tenant_id=%s AND stable_id=%s AND status='pending' RETURNING id, stable_id",
                    (rejected_by, reason, tenant_id, stable_id),
                )
            row = cur.fetchone()
    if row is None:
        raise ErpError("PROPOSAL_STATE", "proposal 不存在或已裁决，不可拒绝")
    audit(conn, tenant_id, "proposal_rejected", actor=rejected_by,
          target=row[1], detail={"reason": reason})
    obs.proposal_rejected(tenant_id=tenant_id, stable_id=row[1])
    return {"status": "rejected", "proposal_id": row[0], "stable_id": row[1]}
