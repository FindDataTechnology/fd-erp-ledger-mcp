"""业务事件层（change: establish-erp-event-kernel）。

职责边界：
- 事件只追加；更正走冲正，定型前修订走 revision 通道（旧值留痕）。
- 两条车道：`ingest`（外部系统原始事实，可直接成为事实）与 `draft`（agent 产出，
  MUST 过确认门；未确认不算事实 —— fact-ledger-kernel R5）。
- 对象余额是投影产物，读得到水位与完整性命明；余额不可直写。
- 读取一律返回完整性声明（declared_total / returned / truncated / watermark），
  让调用方能自证"我拿到的是不是全部"，而不是把截断当成"就这么多"（R8）。

本模块不产分录：过账在 posting 层，缺规则时如实挂"待补"而不是猜科目。
"""
from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .db import fetch_all, fetch_one
from .errors import ErpError
from .tenancy import audit

#: 显式未知的表示。与空串 ""、0、空数组在存储与读回上都可区分（R7/fact-ledger）。
UNKNOWN = "__unknown__"

#: 两条车道的初始状态：外部原始事实即可成为事实；agent 拟稿必须等确认门。
_LANE_INITIAL_STATUS = {"ingest": "accepted", "draft": "pending"}

#: 事件读取统一 JOIN 类型注册表：板块归属（producer）是**类型**的属性，不是事件列。
#: 单表单注册表（design D1/D2）：类型在注册时确定板块，事件只引用类型名。
_EVENT_FROM = "business_events e JOIN event_types et ON et.event_type = e.event_type"
_EVENT_COLS = "e.*, et.producer AS producer"


def _jsonb(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _empty_value(value: Any) -> bool:
    """空串/空数组/None 会被下游读成"没有"，与"未查"不同 —— 一律拒绝。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def _validate_payload(conn: Any, tenant_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """按 event_types.payload_schema 的 required 校验；空值必须显式写成 UNKNOWN。"""
    row = fetch_one(conn, "SELECT payload_schema, requires_posting FROM event_types "
                          "WHERE event_type=%s", (event_type,))
    if row is None:
        raise ErpError("EVENT_UNKNOWN_TYPE", f"事件类型未登记：{event_type}",
                       detail={"event_type": event_type})
    schema = row.get("payload_schema") or {}
    required = schema.get("required") or []
    missing, empty = [], []
    for field in required:
        if field not in payload:
            missing.append(field)
        elif payload[field] is not UNKNOWN and _empty_value(payload[field]):
            empty.append(field)
    if missing:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"事件载荷缺少必填字段：{', '.join(missing)}",
            detail={"missing": missing, "hint_fields": sorted(schema.get("properties", {})) or None},
        )
    if empty:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"事件载荷字段为空值（会被读成「没有」）：{', '.join(empty)}；"
            f"确实未知请显式写 {UNKNOWN!r}",
            detail={"empty_fields": empty, "unknown_sentinel": UNKNOWN},
        )


def append_event(conn: Any, *, settings: Settings, tenant_id: str, event_type: str,
                 stable_id: str, occurred_at: Any, payload: dict[str, Any],
                 lane: str = "ingest", source_ref: str | None = None,
                 provenance: dict[str, Any] | None = None,
                 actor: str = "system",
                 producer: str | None = None) -> dict[str, Any]:
    """写入一条事件。同 (租户, 车道, stable_id) 幂等：重复提交返回首次结果。

    `producer` 形参**不生效**，返回值里的 producer 一律来自类型注册表。
    为什么不由调用方声明：板块归属是「这个类型由哪个板块生产」的注册时事实，
    调用方声明等于自己给自己开板块——营销可以自称 erp 绕开 ERP 侧巡检，
    ERP 也能把账务事实标成营销事实。形参保留只为不破坏既有调用方签名，值直接丢弃。
    """
    if not stable_id or not str(stable_id).strip():
        raise ErpError("INVALID_ARGUMENT", "stable_id 不能为空")
    if lane not in _LANE_INITIAL_STATUS:
        raise ErpError("INVALID_ARGUMENT", "lane 须为 ingest 或 draft",
                       detail={"lane": lane})
    if occurred_at is None:
        raise ErpError("INVALID_ARGUMENT", "occurred_at 不能为空（事实发生时点不可省）")

    del producer  # 调用方声明不采纳，见上方 docstring 与 0011 迁移注释

    _validate_payload(conn, tenant_id, event_type, payload)

    existing = fetch_one(
        conn,
        f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
        "WHERE e.tenant_id=%s AND e.lane=%s AND e.stable_id=%s",
        (tenant_id, lane, stable_id),
    )
    if existing is not None:
        return _event_view(existing, duplicate=True)

    status = _LANE_INITIAL_STATUS[lane]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO business_events(tenant_id, event_type, stable_id, occurred_at, "
                "payload, lane, status, source_ref, provenance) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT (tenant_id, lane, stable_id) DO NOTHING",
                (tenant_id, event_type, stable_id, occurred_at, _jsonb(payload), lane,
                 status, source_ref, _jsonb(provenance or {})),
            )
    row = fetch_one(
        conn,
        f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
        "WHERE e.tenant_id=%s AND e.lane=%s AND e.stable_id=%s",
        (tenant_id, lane, stable_id),
    )
    if row is None:
        # INSERT 成功之后又查不到：只可能是它被别的事务清掉了（维护会话的 TRUNCATE）
        # 或者类型注册表在两步之间被删行导致 JOIN 落空。原实现直接 row.get(...)，
        # 在这里抛 AttributeError——错误路径上的未定义状态把「行没了」伪装成代码 bug。
        raise ErpError(
            "NOT_FOUND",
            f"事件写入后读不回：{stable_id}（租户 {tenant_id}，车道 {lane}）；"
            "常见原因是并发维护操作清空了表，请重试并确认无并发 TRUNCATE",
            detail={"stable_id": stable_id, "lane": lane, "event_type": event_type},
        )
    audit(conn, tenant_id, "event_appended", actor=actor, target=stable_id,
          detail={"event_type": event_type, "lane": lane, "status": status,
                  "producer": row.get("producer")})
    return _event_view(row, duplicate=False)


def decide_event(conn: Any, *, tenant_id: str, stable_id: str, lane: str,
                 decision: str, actor: str = "human", note: str = "") -> dict[str, Any]:
    """确认门：draft 车道事件的 accepted / rejected。唯一改变事件状态的人工入口。"""
    if decision not in ("accepted", "rejected"):
        raise ErpError("INVALID_ARGUMENT", "decision 须为 accepted 或 rejected")
    row = fetch_one(conn, f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
                          "WHERE e.tenant_id=%s AND e.lane=%s AND e.stable_id=%s",
                    (tenant_id, lane, stable_id))
    if row is None:
        raise ErpError("NOT_FOUND", f"事件不存在：{stable_id}", detail={"lane": lane})
    if lane != "draft":
        raise ErpError("EVENT_STATE", "只有 draft 车道的事件需要确认；ingest 车道写入即是事实")
    if row["status"] != "pending":
        raise ErpError("EVENT_STATE", f"事件状态为 {row['status']}，仅 pending 可确认/拒绝",
                       detail={"status": row["status"]})
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE business_events SET status=%s, decided_at=now(), decided_by=%s "
                "WHERE id=%s", (decision, actor, row["id"]),
            )
    audit(conn, tenant_id, "event_decided", actor=actor, target=stable_id,
          detail={"decision": decision, "note": note})
    return _event_view(fetch_one(conn, f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
                                      "WHERE e.id=%s", (row["id"],)), duplicate=False)


def reverse_event(conn: Any, *, tenant_id: str, stable_id: str, lane: str,
                  reversal_stable_id: str, reversal_payload: dict[str, Any] | None = None,
                  actor: str = "human", note: str = "") -> dict[str, Any]:
    """冲正：新增一条反向事件并关联原事件；原事件置 reversed。原事件内容不改。"""
    original = fetch_one(conn, f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
                               "WHERE e.tenant_id=%s AND e.lane=%s AND e.stable_id=%s",
                         (tenant_id, lane, stable_id))
    if original is None:
        raise ErpError("NOT_FOUND", f"原事件不存在：{stable_id}")
    if original["status"] != "accepted":
        raise ErpError("EVENT_STATE",
                       f"仅已接受（accepted）的事件可冲正，当前 {original['status']}",
                       detail={"status": original["status"]})
    if not reversal_stable_id or reversal_stable_id == stable_id:
        raise ErpError("INVALID_ARGUMENT", "冲正事件须有独立 stable_id（不得与原事件相同）")

    payload = reversal_payload if reversal_payload is not None else dict(original["payload"])
    existing = fetch_one(conn, f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
                               "WHERE e.tenant_id=%s AND e.lane=%s AND e.stable_id=%s",
                         (tenant_id, lane, reversal_stable_id))
    if existing is None:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO business_events(tenant_id, event_type, stable_id, occurred_at, "
                    "payload, lane, status, source_ref, reversal_of, provenance) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb,%s,'accepted',%s,%s,%s::jsonb)",
                    (tenant_id, original["event_type"], reversal_stable_id,
                     original["occurred_at"], _jsonb(payload), lane,
                     original["source_ref"], stable_id,
                     _jsonb({"reversal_note": note})),
                )
        existing = fetch_one(conn, f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
                                   "WHERE e.tenant_id=%s AND e.lane=%s AND e.stable_id=%s",
                             (tenant_id, lane, reversal_stable_id))
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("UPDATE business_events SET status='reversed', decided_at=now(), "
                        "decided_by=%s WHERE id=%s", (actor, original["id"]))
    audit(conn, tenant_id, "event_reversed", actor=actor, target=stable_id,
          detail={"reversal_stable_id": reversal_stable_id})
    return {"original": _event_view(fetch_one(conn, f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
                                                    "WHERE e.id=%s", (original["id"],)),
                                    duplicate=False),
            "reversal": _event_view(existing, duplicate=False)}


def revise_event(conn: Any, *, tenant_id: str, stable_id: str, lane: str,
                 payload: dict[str, Any], actor: str, reason: str = "") -> dict[str, Any]:
    """定型前修订：保留旧值 + 修订来源 + 时点（旧值不消失）。"""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT erp_revise_event(%s,%s,%s,%s::jsonb,%s,%s)",
                        (tenant_id, stable_id, lane, _jsonb(payload), actor, reason))
    audit(conn, tenant_id, "event_revised", actor=actor, target=stable_id,
          detail={"reason": reason})
    row = fetch_one(conn, f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
                          "WHERE e.tenant_id=%s AND e.lane=%s AND e.stable_id=%s",
                    (tenant_id, lane, stable_id))
    revisions = fetch_all(conn, "SELECT old_payload, new_payload, revised_by, reason, revised_at "
                                "FROM event_revisions WHERE event_id=%s ORDER BY revised_at",
                          (row["id"],))
    return {**_event_view(row, duplicate=False), "revisions": revisions}


def query_events(conn: Any, *, tenant_id: str, event_type: str | None = None,
                 lane: str | None = None, status: str | None = None,
                 producer: str | None = None,
                 limit: int = 100, after_event_id: int = 0) -> dict[str, Any]:
    """查询事件；返回值自带完整性声明（R8：读取必须声明完整性与降级）。

    `producer` 默认 None = 不过滤：同一租户内事实跨板块可见，不设板块墙（design D6）。
    需要只看某个板块时显式传 'erp' / 'marketing'——否则营销事件会淹掉 ERP 侧巡检。
    不认识的 producer 值返回空结果 + declared_total=0，而不是报错：这是过滤条件，
    不是入参授权（与 lane/status 同口径）。
    """
    where = ["e.tenant_id = %s", "e.id > %s"]
    params: list[Any] = [tenant_id, after_event_id]
    if event_type:
        where.append("e.event_type = %s")
        params.append(event_type)
    if lane:
        where.append("e.lane = %s")
        params.append(lane)
    if status:
        where.append("e.status = %s")
        params.append(status)
    if producer:
        where.append("et.producer = %s")
        params.append(producer)
    clause = " AND ".join(where)

    total = fetch_one(conn, f"SELECT COUNT(*) AS n FROM {_EVENT_FROM} WHERE {clause}",
                      tuple(params))["n"]
    rows = fetch_all(conn, f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} WHERE {clause} "
                           f"ORDER BY e.id LIMIT %s", tuple(params + [limit]))
    watermark = fetch_one(conn, f"SELECT COALESCE(MAX(e.id),0) AS w FROM {_EVENT_FROM} "
                                f"WHERE {clause}", tuple(params))["w"]
    return {
        "items": [_event_view(r, duplicate=False) for r in rows],
        "completeness": _completeness(total, len(rows), watermark),
    }


def pending_projection_events(conn: Any, *, tenant_id: str, after_event_id: int = 0,
                              limit: int = 500) -> dict[str, Any]:
    """续跑闸（R7）：投影只处理水位之后的事件，重跑不重做已投影部分。"""
    rows = fetch_all(
        conn,
        f"SELECT {_EVENT_COLS} FROM {_EVENT_FROM} "
        "WHERE e.tenant_id=%s AND e.id > %s AND e.status='accepted' "
        "  AND NOT EXISTS (SELECT 1 FROM object_events oe WHERE oe.event_id = e.id) "
        "ORDER BY e.id LIMIT %s",
        (tenant_id, after_event_id, limit),
    )
    total = fetch_one(
        conn,
        "SELECT COUNT(*) AS n FROM business_events e "
        "WHERE e.tenant_id=%s AND e.id > %s AND e.status='accepted' "
        "  AND NOT EXISTS (SELECT 1 FROM object_events oe WHERE oe.event_id = e.id)",
        (tenant_id, after_event_id),
    )["n"]
    watermark = fetch_one(conn, "SELECT COALESCE(MAX(id),0) AS w FROM business_events "
                                "WHERE tenant_id=%s", (tenant_id,))["w"]
    return {
        "items": [_event_view(r, duplicate=False) for r in rows],
        "completeness": _completeness(total, len(rows), watermark),
    }


def project_object_event(conn: Any, *, tenant_id: str, event_id: int, object_type: str,
                         object_key: str, quantity_delta: Any, unit: str,
                         dimension: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一条已接受事件投影到对象数量面。幂等：同 (事件, 对象, 维度) 不产生第二次增量。

    只有 accepted 的事件是事实；pending 的拟稿事件 MUST NOT 参与任何派生投影。
    """
    event = fetch_one(conn, "SELECT status FROM business_events WHERE id=%s AND tenant_id=%s",
                      (event_id, tenant_id))
    if event is None:
        raise ErpError("NOT_FOUND", f"事件不存在：{event_id}")
    if event["status"] != "accepted":
        raise ErpError("EVENT_STATE",
                       f"事件 {event_id} 状态为 {event['status']}，只有 accepted 的事实可投影",
                       detail={"event_id": event_id, "status": event["status"]})
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT erp_project_object_event(%s,%s,%s,%s::jsonb,%s,%s) AS qty",
                        (event_id, object_type, object_key, _jsonb(dimension or {}),
                         quantity_delta, unit))
    audit(conn, tenant_id, "object_projected", target=f"{object_type}/{object_key}",
          detail={"event_id": event_id, "delta": str(quantity_delta), "unit": unit})
    return query_object_balance(conn, tenant_id=tenant_id, object_type=object_type,
                               object_key=object_key, dimension=dimension)


def query_object_balance(conn: Any, *, tenant_id: str, object_type: str, object_key: str,
                         dimension: dict[str, Any] | None = None) -> dict[str, Any]:
    """读对象余额；带水位 + 重算校验（重算是校验手段，不是读路径）。"""
    dim = dimension or {}
    row = fetch_one(
        conn,
        "SELECT quantity, unit, as_of_event_id, computed_at FROM object_balances "
        "WHERE tenant_id=%s AND object_type=%s AND object_key=%s AND dimension=%s::jsonb",
        (tenant_id, object_type, object_key, _jsonb(dim)),
    )
    recomputed = fetch_one(
        conn, "SELECT * FROM erp_recompute_object_balance(%s,%s,%s,%s::jsonb)",
        (tenant_id, object_type, object_key, _jsonb(dim)),
    )
    if row is None:
        return {
            "object_type": object_type, "object_key": object_key, "dimension": dim,
            "quantity": None, "unit": None,
            "completeness": {
                "declared_total": 0, "returned": 0, "truncated": False,
                "watermark": recomputed["as_of_event_id"] if recomputed else 0,
                "state": "absent",
            },
            "recomputed": recomputed,
        }
    drift = (row["quantity"] != recomputed["quantity"]
             or row["as_of_event_id"] != recomputed["as_of_event_id"])
    return {
        "object_type": object_type, "object_key": object_key, "dimension": dim,
        "quantity": row["quantity"], "unit": row["unit"],
        "as_of_event_id": row["as_of_event_id"], "computed_at": row["computed_at"],
        "completeness": {
            "declared_total": 1, "returned": 1, "truncated": False,
            "watermark": row["as_of_event_id"],
            "state": "complete" if not drift else "drifted",
        },
        "recomputed": recomputed,
        "drift": drift,
    }


def _completeness(total: int, returned: int, watermark: int) -> dict[str, Any]:
    return {
        "declared_total": total,
        "returned": returned,
        "truncated": returned < total,
        "watermark": watermark,
        # 降级声明：truncated 为真时调用方 MUST NOT 把结果当成全部
        "state": "complete" if returned >= total else "partial",
    }


def _event_view(row: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
    view = {
        "id": row["id"],
        "event_type": row["event_type"],
        "stable_id": row["stable_id"],
        "occurred_at": row["occurred_at"],
        "lane": row["lane"],
        "status": row["status"],
        # 板块归属来自类型注册表（JOIN event_types），不是调用方声明
        "producer": row.get("producer"),
        "source_ref": row["source_ref"],
        "reversal_of": row["reversal_of"],
        "payload": row["payload"],
        "provenance": row.get("provenance") or {},
        "decided_at": row.get("decided_at"),
        "decided_by": row.get("decided_by"),
    }
    if duplicate:
        # 幂等命中：明示"这是既有记录"，避免调用方误以为是新落的一条
        view["duplicate"] = True
    return view
