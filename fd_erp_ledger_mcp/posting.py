"""过账层：事件 → 复式分录（change: establish-erp-event-kernel 3.2-3.4）。

边界（三条都不能松）：
- 规则只决定**科目与金额算法**，不承担业务分支：分支留在事件类型里。允许规则
  里出现"到货走这个科目、领用走那个科目"就等于把内核复制进规则，规则会按客户
  膨胀成第二个内核。
- 缺规则时**不猜科目**：事件挂进 posting_gaps 待补，账本余额不变，状态具名。
- 过账是已接受事实的**确定性派生**，不是第二次确认门：事件已在事件层过门
  （draft 车道经 confirm，ingest 车道来源系统即权威）。但过账仍受机器车道白名单
  约束——规则里的科目必须落在允许集内，借贷必平仍由数据库触发器兜底。
"""
from __future__ import annotations

from typing import Any

from .config import Settings
from .db import fetch_all, fetch_one
from .errors import ErpError, map_psycog_error
from .ledger import _normalize_lines, post_lines
from .tenancy import audit

POSTING_ACTOR = "posting-engine"

#: 机器车道可落账的科目白名单。到货/领用/调整涉及存货与应付/成本，实际项目按需扩；
#: 现金(1000)、预收(2100)、权益(3000/3100) 一律不在集内 —— 执行面与资金面归外部系统。
POSTING_ACCOUNTS = frozenset({"1300", "1405", "2202", "4000", "5100", "5200", "6401"})


def _resolve_amount(expr: str, payload: dict[str, Any]) -> Any:
    """金额表达式：`payload.<key>` 取载荷字段；其余按字面数值。"""
    if not isinstance(expr, str):
        return expr
    if expr.startswith("payload."):
        key = expr[len("payload."):]
        if key not in payload:
            raise ErpError("POSTING_RULE_MISSING",
                           f"过账规则引用的载荷字段不存在：{key}",
                           detail={"missing_field": key, "available": sorted(payload)})
        return payload[key]
    try:
        return float(expr)
    except ValueError as exc:
        raise ErpError("INVALID_ARGUMENT",
                       f"过账规则金额表达式无法解析：{expr!r}",
                       detail={"expression": expr}) from exc


def _validate_rule(rule: dict[str, Any]) -> list[dict[str, Any]]:
    lines = (rule or {}).get("lines")
    if not isinstance(lines, list) or len(lines) < 2:
        raise ErpError("INVALID_ARGUMENT", "过账规则至少需要借贷两行",
                       detail={"lines": lines})
    bad_accounts, sides = [], set()
    for ln in lines:
        if not isinstance(ln, dict) or "account_code" not in ln or "direction" not in ln:
            raise ErpError("INVALID_ARGUMENT", "过账规则行缺少 account_code/direction",
                           detail={"line": ln})
        if ln["account_code"] not in POSTING_ACCOUNTS:
            bad_accounts.append(ln["account_code"])
        if ln["direction"] not in ("debit", "credit"):
            raise ErpError("INVALID_ARGUMENT", f"direction 非法：{ln['direction']}")
        if "amount_from" not in ln:
            raise ErpError("INVALID_ARGUMENT", "过账规则行缺少 amount_from", detail={"line": ln})
        sides.add(ln["direction"])
    if bad_accounts:
        raise ErpError("INVALID_ARGUMENT",
                       f"过账规则含机器车道白名单外科目：{', '.join(bad_accounts)}",
                       detail={"forbidden_accounts": bad_accounts,
                               "allowed_accounts": sorted(POSTING_ACCOUNTS)})
    if sides != {"debit", "credit"}:
        raise ErpError("INVALID_ARGUMENT", "过账规则必须同时含借方与贷方")
    return lines


def submit_posting_rule(conn: Any, *, tenant_id: str, event_type: str,
                        rule: dict[str, Any], source_ref: str | None = None,
                        actor: str = "agent") -> dict[str, Any]:
    """提交规则草稿。草稿不参与过账，必须经 approve 才生效。"""
    _validate_rule(rule)
    row = fetch_one(conn, "SELECT MAX(version) AS v FROM posting_rules "
                          "WHERE tenant_id=%s AND event_type=%s", (tenant_id, event_type))
    version = (row["v"] or 0) + 1
    import json
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO posting_rules(tenant_id, event_type, version, status, rule, "
                "source_ref, created_by) VALUES (%s,%s,%s,'draft',%s::jsonb,%s,%s) "
                "ON CONFLICT (tenant_id, event_type, version) DO NOTHING",
                (tenant_id, event_type, version,
                 json.dumps(rule, ensure_ascii=False), source_ref, actor))
    audit(conn, tenant_id, "posting_rule_drafted", actor=actor,
          target=f"{event_type}@v{version}")
    return fetch_one(conn, "SELECT * FROM posting_rules WHERE tenant_id=%s "
                           "AND event_type=%s AND version=%s",
                     (tenant_id, event_type, version))


def approve_posting_rule(conn: Any, *, tenant_id: str, event_type: str, version: int,
                         approved_by: str) -> dict[str, Any]:
    """审批上线：退役旧生效版本，本版本生效。升版不重写历史分录。"""
    target = fetch_one(conn, "SELECT * FROM posting_rules WHERE tenant_id=%s "
                             "AND event_type=%s AND version=%s",
                       (tenant_id, event_type, version))
    if target is None:
        raise ErpError("NOT_FOUND", f"过账规则不存在：{event_type}@v{version}")
    if target["status"] != "draft":
        raise ErpError("INVALID_ARGUMENT",
                       f"规则状态为 {target['status']}，仅 draft 可审批上线",
                       detail={"status": target["status"]})
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("UPDATE posting_rules SET status='retired', retired_at=now() "
                        "WHERE tenant_id=%s AND event_type=%s AND status='active'",
                        (tenant_id, event_type))
            cur.execute("UPDATE posting_rules SET status='active', approved_by=%s, "
                        "approved_at=now() WHERE id=%s", (approved_by, target["id"]))
    audit(conn, tenant_id, "posting_rule_approved", actor=approved_by,
          target=f"{event_type}@v{version}")
    return fetch_one(conn, "SELECT * FROM posting_rules WHERE id=%s", (target["id"],))


def post_event(conn: Any, *, settings: Settings, tenant_id: str,
               event_id: int) -> dict[str, Any]:
    """把一条已接受的事件过账。幂等：同一事件只过一次账。"""
    event = fetch_one(conn, "SELECT * FROM business_events WHERE id=%s AND tenant_id=%s",
                      (event_id, tenant_id))
    if event is None:
        raise ErpError("NOT_FOUND", f"事件不存在：{event_id}")
    if event["status"] != "accepted":
        raise ErpError("EVENT_STATE",
                       f"事件状态为 {event['status']}，只有 accepted 的事件可过账",
                       detail={"status": event["status"]})

    link = fetch_one(conn, "SELECT * FROM posting_links WHERE event_id=%s", (event_id,))
    if link is not None:
        return {"posted": False, "duplicate": True, "txn_id": link["txn_id"],
                "event_id": event_id, "stable_id": event["stable_id"]}

    rule = fetch_one(conn, "SELECT * FROM posting_rules WHERE tenant_id=%s "
                           "AND event_type=%s AND status='active'",
                     (tenant_id, event["event_type"]))
    if rule is None:
        meta = fetch_one(conn, "SELECT requires_posting FROM event_types WHERE event_type=%s",
                         (event["event_type"],))
        if meta and not meta["requires_posting"]:
            return {"posted": False, "reason": "not_required", "event_id": event_id,
                    "event_type": event["event_type"]}
        detail = {"event_type": event["event_type"], "event_id": event_id,
                  "required_accounts": sorted(POSTING_ACCOUNTS)}
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO posting_gaps(tenant_id, event_id, event_type, reason, detail) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb) "
                    "ON CONFLICT (event_id) WHERE resolved_at IS NULL DO NOTHING",
                    (tenant_id, event_id, event["event_type"],
                     "缺过账规则：该事件类型没有 active 规则，故未落账（不猜科目）",
                     _json_dumps(detail)))
        raise ErpError("POSTING_RULE_MISSING",
                       f"事件 {event_id}（{event['event_type']}）没有生效的过账规则，"
                       f"已挂入待补清单、账本余额不变",
                       detail=detail)

    lines = []
    for i, spec in enumerate(rule["rule"]["lines"], start=1):
        amount = _resolve_amount(spec["amount_from"], event["payload"])
        currency = spec.get("currency") or event["payload"].get("currency") or settings.base_currency
        lines.append({"line_no": i, "account_code": spec["account_code"],
                      "direction": spec["direction"], "amount": amount,
                      "currency": currency})

    try:
        normalized = _normalize_lines(lines, settings=settings, conn=conn)
        posted = post_lines(conn, settings=settings, tenant_id=tenant_id,
                           stable_id=f"{event['stable_id']}#post",
                           lines=normalized, source_ref=f"event:{event_id}",
                           confirmed_by=POSTING_ACTOR)
    except ErpError:
        raise
    except Exception as exc:
        raise map_psycog_error(exc) from exc

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO posting_links(tenant_id, event_id, rule_id, txn_id, lines) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING",
                (tenant_id, event_id, rule["id"], posted["txn_id"], posted["lines"]))
            cur.execute("UPDATE posting_gaps SET resolved_at=now() "
                        "WHERE event_id=%s AND resolved_at IS NULL", (event_id,))
    audit(conn, tenant_id, "event_posted", actor=POSTING_ACTOR,
          target=event["stable_id"], detail={"txn_id": posted["txn_id"],
                                            "rule": f"{event['event_type']}@v{rule['version']}"})
    return {"posted": True, "event_id": event_id, "stable_id": event["stable_id"],
            "txn_id": posted["txn_id"], "lines": posted["lines"],
            "rule_version": rule["version"]}


def list_posting_gaps(conn: Any, *, tenant_id: str) -> dict[str, Any]:
    """待补清单：谁在等规则、等哪条规则。"""
    rows = fetch_all(
        conn,
        "SELECT g.*, e.stable_id, e.occurred_at FROM posting_gaps g "
        "JOIN business_events e ON e.id = g.event_id "
        "WHERE g.tenant_id=%s AND g.resolved_at IS NULL ORDER BY g.id", (tenant_id,))
    return {"items": rows, "completeness": {"declared_total": len(rows),
                                           "returned": len(rows), "truncated": False,
                                           "state": "complete"}}


def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)
