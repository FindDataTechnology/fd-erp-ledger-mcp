"""计费规则引擎（erp-vertical-templates 任务 1.x）。

设计要点（与账本内核衔接）：
- 规则版本化：同 rule_key 版本递增；状态机 draft → pending_approval → active/retired。
  审批（pending_approval → active）复用 proposal 确认语义：MUST 经独立的人工确认动作
  （approve_billing_rule，approved_by 不得为程序角色），不接受程序自审自批。
- 上线拦截（conflict gate）：同收费维度（fee_item）的 active 规则，区间相交且无显式
  优先级、或生效期存在空洞 → 审批拒绝（RULE_CONFLICT），未处置 MUST NOT active。
- 无出处计价参数拒绝：config.rate_source 缺失/空白 → RATE_UNSOURCED（仿 FX_UNSOURCED）。
- 事件计费：bill_event 按 active 规则匹配计价 → 写 fee_details（append-only）；
  同 stable_id 重推返回首次结果（UNIQUE(tenant_id, stable_id) 兜底）。
- 计费快照：每行费用锁定 rule_id + rule_version + 基础数据快照（snapshot jsonb），
  可离线确定性重算（get_fee_detail.snapshot_replay）。
- 红冲：reverse_fee 生成反向明细行（status='reversed' + reversal_of 关联），
  原行原样保留；红冲本身也是 fee_details 行，无 UPDATE/DELETE 路径。
- 四层溯源：费用项 → 业务单据（event.refs）→ 作业事件（event）→ 计费规则版本
  （rule_id + version + rate_source）。
- 与账本衔接：计费明细落账 = 组成分录 proposal 走既有 submit_proposal
  （submitted_by="billing-engine"，科目白名单闸在 proposals 层强制：限 1300/4000/
  费用类 5100/5200，禁 1000/2100/3000/3100）；计费引擎绝不直写 journal_entries，
  落账仍只能经 confirm_proposal 确认门。
- 多币种折算（design D7）：组分录时按 fx_facts 的登记事实折算，费用明细同时落
  原币金额 / 本位币金额 / 汇率 / 时点 / 出处；无出处或与登记不符 → FX_UNSOURCED
  且零分录、零费用行（汇率在任何写入前解析）。MUST NOT 再静默跳过非本位币费用。
- 覆盖缺失阻断（design D9）：收费项登记（billing_fee_items）的「在用集合」减去
  「active 规则集合」非空 → 审批上线与事件/期间计费一律拒绝并点名
  （FEE_COVERAGE_MISSING）；不以日志告警后放行。
- 日终快照计费：bill_period_snapshots（存储费按日终库存快照 × 费率；单期间批量、
  幂等键 = bill-period:<period>，覆写 fee_details 的 (tenant, stable_id) 唯一键），
  同样产出 fee_details 行 + 一条机器车道 proposal。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .config import Settings
from .db import fetch_all, fetch_one
from .errors import ErpError
from .fx import resolve_rate
from .proposals import submit_proposal
from .tenancy import audit
from . import observability as obs

Q6 = Decimal("0.000001")
_MODES = ("unit", "percent", "tiered")
_FEE_TYPES = ("revenue", "expense")


# ---------------------------------------------------------------------------
# 纯计价（确定性、可重算；不触库）
# ---------------------------------------------------------------------------

def _dec(value: Any, *, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ErpError("INVALID_ARGUMENT", f"{what} 不是数字：{value!r}") from exc


def _parse_date(value: Any, *, what: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception as exc:
        raise ErpError("INVALID_ARGUMENT", f"{what} 不是合法日期：{value!r}") from exc


def _cfg_date(value: Any, *, what: str) -> date | None:
    """规则 config 中的可选日期；None/空白 = 开区间端点。"""
    if value is None or str(value).strip() == "":
        return None
    return _parse_date(value, what=what)


def compute_fee_with_breakdown(config: dict[str, Any],
                               measures: dict[str, Any]) -> tuple[Decimal, dict[str, Any]]:
    """按锁定 config + 基础数据确定性计价；返回 (金额, 计算过程摘要)。

    计价模式：
      unit    金额 = unit_price × measures[measure]（默认 quantity）
      percent 金额 = percent_rate/100 × measures[measure]（默认 amount）
      tiered  分档累计：tiers=[{up_to, unit_price}, ...]，末档 up_to 须为 null
    组合：cap_amount 封顶、minimum_amount 最低消费（cap < minimum 拒绝）。
    """
    mode = str(config.get("mode") or "")
    measure_key = str(config.get("measure") or ("amount" if mode == "percent" else "quantity"))
    if not isinstance(measures, dict) or measure_key not in measures:
        raise ErpError("INVALID_ARGUMENT", f"事件缺少计费量纲 measures[{measure_key!r}]",
                       detail={"measure": measure_key,
                               "measures_keys": sorted(measures) if isinstance(measures, dict) else None})
    qty = _dec(measures[measure_key], what=f"measures[{measure_key}]")
    if qty < 0:
        raise ErpError("INVALID_ARGUMENT", f"计费量纲 measures[{measure_key}] 不得为负")

    raw = Decimal(0)
    breakdown_tiers: list[dict[str, Any]] | None = None
    if mode == "unit":
        raw = _dec(config["unit_price"], what="unit_price") * qty
    elif mode == "percent":
        raw = _dec(config["percent_rate"], what="percent_rate") / Decimal(100) * qty
    elif mode == "tiered":
        remaining = qty
        prev = Decimal(0)
        tier_used: list[dict[str, Any]] = []
        for t in config["tiers"]:
            up = None if t.get("up_to") is None else _dec(t["up_to"], what="tiers.up_to")
            band = remaining if up is None else up - prev
            used = min(remaining, band)
            used = used if used > 0 else Decimal(0)
            line_amount = used * _dec(t["unit_price"], what="tiers.unit_price")
            raw += line_amount
            remaining -= used
            tier_used.append({"up_to": str(up) if up is not None else None,
                              "unit_price": str(t["unit_price"]),
                              "quantity_used": str(used.quantize(Q6))})
            if up is not None:
                prev = up
            if remaining <= 0:
                break
        if remaining > 0:
            raise ErpError("INVALID_ARGUMENT", "阶梯未覆盖全部计费量（末档 up_to 须为 null）",
                           detail={"uncovered": str(remaining.quantize(Q6))})
        breakdown_tiers = tier_used
    else:
        raise ErpError("INVALID_ARGUMENT", f"计价 mode 非法：{mode!r}（须 unit/percent/tiered）")

    amount = raw.quantize(Q6, rounding=ROUND_HALF_UP)
    applied: dict[str, str] = {}
    if config.get("cap_amount") is not None:
        cap = _dec(config["cap_amount"], what="cap_amount")
        if amount > cap:
            applied["cap_amount"] = str(cap)
            amount = cap.quantize(Q6)
    if config.get("minimum_amount") is not None:
        minimum = _dec(config["minimum_amount"], what="minimum_amount")
        if amount < minimum:
            applied["minimum_amount"] = str(minimum)
            amount = minimum.quantize(Q6)
    breakdown = {"mode": mode, "measure": measure_key,
                 "quantity": str(qty.quantize(Q6)), "raw_amount": str(raw.quantize(Q6)),
                 "applied_limits": applied}
    if breakdown_tiers is not None:
        breakdown["tiers_used"] = breakdown_tiers
    return amount, breakdown


def compute_fee(config: dict[str, Any], measures: dict[str, Any]) -> Decimal:
    """确定性计价（快照重算口径）。"""
    return compute_fee_with_breakdown(config, measures)[0]


# ---------------------------------------------------------------------------
# config 校验（无出处费率拒绝在这里强制）
# ---------------------------------------------------------------------------

def validate_config(config: Any, *, settings: Settings) -> dict[str, Any]:
    """校验并返回规则 config（浅拷贝）。rate_source 缺失 → RATE_UNSOURCED。"""
    if not isinstance(config, dict):
        raise ErpError("INVALID_ARGUMENT", "规则 config 须为对象")
    cfg = dict(config)

    rate_source = str(cfg.get("rate_source") or "").strip()
    if not rate_source:
        raise ErpError(
            "RATE_UNSOURCED",
            "规则费率缺少出处（config.rate_source 为空）；拒绝无出处计价参数",
            detail={"missing_field": "rate_source",
                    "expected": "费率来源引用（如价目表编号/合同号/审批单号）"},
        )
    cfg["rate_source"] = rate_source

    fee_item = str(cfg.get("fee_item") or "").strip()
    if not fee_item:
        raise ErpError("INVALID_ARGUMENT", "config.fee_item（收费维度）不能为空")
    cfg["fee_item"] = fee_item
    event_kind = str(cfg.get("event_kind") or "").strip()
    if not event_kind:
        raise ErpError("INVALID_ARGUMENT", "config.event_kind（匹配的事件类型）不能为空")
    cfg["event_kind"] = event_kind

    rule_key = str(cfg.get("rule_key") or "").strip() or fee_item
    cfg["rule_key"] = rule_key

    mode = str(cfg.get("mode") or "").strip()
    if mode not in _MODES:
        raise ErpError("INVALID_ARGUMENT", f"config.mode 须为 {'/'.join(_MODES)}，实为 {mode!r}")
    cfg["mode"] = mode

    fee_type = str(cfg.get("fee_type") or "revenue").strip()
    if fee_type not in _FEE_TYPES:
        raise ErpError("INVALID_ARGUMENT", f"config.fee_type 须为 revenue/expense，实为 {fee_type!r}")
    cfg["fee_type"] = fee_type

    if mode == "unit":
        if cfg.get("unit_price") is None:
            raise ErpError("INVALID_ARGUMENT", "unit 模式须提供 config.unit_price")
        if _dec(cfg["unit_price"], what="unit_price") < 0:
            raise ErpError("INVALID_ARGUMENT", "config.unit_price 不得为负")
    elif mode == "percent":
        if cfg.get("percent_rate") is None:
            raise ErpError("INVALID_ARGUMENT", "percent 模式须提供 config.percent_rate")
        if _dec(cfg["percent_rate"], what="percent_rate") <= 0:
            raise ErpError("INVALID_ARGUMENT", "config.percent_rate 须 > 0")
    else:  # tiered
        tiers = cfg.get("tiers")
        if not isinstance(tiers, list) or not tiers:
            raise ErpError("INVALID_ARGUMENT", "tiered 模式须提供非空 config.tiers")
        prev: Decimal | None = None
        for i, t in enumerate(tiers):
            if not isinstance(t, dict) or t.get("unit_price") is None:
                raise ErpError("INVALID_ARGUMENT", f"tiers[{i}] 缺 unit_price")
            if _dec(t["unit_price"], what="tiers.unit_price") < 0:
                raise ErpError("INVALID_ARGUMENT", f"tiers[{i}].unit_price 不得为负")
            if t.get("up_to") is None:
                if i != len(tiers) - 1:
                    raise ErpError("INVALID_ARGUMENT",
                                   f"tiers[{i}].up_to=null 只允许出现在末档")
            else:
                up = _dec(t["up_to"], what="tiers.up_to")
                if prev is not None and up <= prev:
                    raise ErpError("INVALID_ARGUMENT",
                                   f"tiers[{i}].up_to 须严格递增（> {prev}）")
                prev = up
        if tiers[-1].get("up_to") is not None:
            raise ErpError("INVALID_ARGUMENT", "末档 tiers.up_to 须为 null（覆盖全部余量）")

    if cfg.get("cap_amount") is not None:
        if _dec(cfg["cap_amount"], what="cap_amount") < 0:
            raise ErpError("INVALID_ARGUMENT", "config.cap_amount 不得为负")
    if cfg.get("minimum_amount") is not None:
        if _dec(cfg["minimum_amount"], what="minimum_amount") < 0:
            raise ErpError("INVALID_ARGUMENT", "config.minimum_amount 不得为负")
    if cfg.get("cap_amount") is not None and cfg.get("minimum_amount") is not None:
        if _dec(cfg["minimum_amount"], what="minimum_amount") > _dec(cfg["cap_amount"], what="cap_amount"):
            raise ErpError("INVALID_ARGUMENT", "config.minimum_amount 不得大于 cap_amount")

    if cfg.get("effective_from") is not None:
        _cfg_date(cfg["effective_from"], what="effective_from")
    if cfg.get("effective_to") is not None:
        _cfg_date(cfg["effective_to"], what="effective_to")
    if cfg.get("effective_from") and cfg.get("effective_to"):
        if _cfg_date(cfg["effective_from"], what="effective_from") > \
                _cfg_date(cfg["effective_to"], what="effective_to"):
            raise ErpError("INVALID_ARGUMENT", "effective_from 不得晚于 effective_to")
    if cfg.get("priority") is not None:
        _dec(cfg["priority"], what="priority")
    cfg["currency"] = str(cfg.get("currency") or settings.base_currency).strip()
    # 非本位币计价可以在规则里指定汇率出处（fx_facts.source）；显式给了空白 → 视为
    # 「无出处」拒绝，而不是悄悄回落到默认源（出处纪律同 config.rate_source）。
    if "fx_source" in cfg and cfg["fx_source"] is not None:
        fx_source = str(cfg["fx_source"]).strip()
        if not fx_source:
            raise ErpError(
                "FX_UNSOURCED",
                "规则 config.fx_source 为空；拒绝无出处汇率（删除该字段以使用默认汇率源）",
                detail={"missing_field": "fx_source",
                        "currency": cfg["currency"],
                        "default_source": settings.fx_source},
            )
        cfg["fx_source"] = fx_source
    return cfg


def _fee_currency(cfg: dict[str, Any], *, settings: Settings) -> str:
    return str(cfg.get("currency") or settings.base_currency)


# ---------------------------------------------------------------------------
# 规则状态机 + 冲突检测
# ---------------------------------------------------------------------------

def _rule_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule_id": row["id"],
        "rule_key": row["rule_key"],
        "version": row["version"],
        "status": row["status"],
        "fee_item": row["config"].get("fee_item") if isinstance(row["config"], dict) else None,
        "event_kind": row["config"].get("event_kind") if isinstance(row["config"], dict) else None,
        "mode": row["config"].get("mode") if isinstance(row["config"], dict) else None,
        "rate_source": row["config"].get("rate_source") if isinstance(row["config"], dict) else None,
        "effective_from": str(row["config"]["effective_from"]) if isinstance(row["config"], dict) and row["config"].get("effective_from") else None,
        "effective_to": str(row["config"]["effective_to"]) if isinstance(row["config"], dict) and row["config"].get("effective_to") else None,
        "priority": row["config"].get("priority") if isinstance(row["config"], dict) else None,
        "source_ref": row["source_ref"],
        "submitted_by": row["submitted_by"],
        "approved_by": row["approved_by"],
        "approved_at": str(row["approved_at"]) if row.get("approved_at") else None,
        "created_at": str(row["created_at"]),
    }


def submit_billing_rule(conn: Any, *, settings: Settings, tenant_id: str,
                        config: dict[str, Any], source_ref: str | None = None,
                        submitted_by: str = "ops") -> dict[str, Any]:
    """登记新版本计费规则：draft → pending_approval（submit 动作本身）。

    同 rule_key 版本递增；config 经 validate_config（无出处费率在此拒绝）。
    """
    cfg = validate_config(config, settings=settings)
    rule_key = str(cfg["rule_key"])
    provenance = {"rate_source": cfg["rate_source"], "submitted_by": submitted_by,
                  "validated_fields": ["fee_item", "event_kind", "mode", "rate_source"]}
    for _attempt in range(4):  # 并发同 key 提交时按最新版本重试
        row_max = fetch_one(
            conn, "SELECT COALESCE(MAX(version),0) AS v FROM billing_rules "
                  "WHERE tenant_id=%s AND rule_key=%s", (tenant_id, rule_key))
        version = int(row_max["v"]) + 1
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO billing_rules(tenant_id, rule_key, version, config, "
                    "status, source_ref, provenance, submitted_by) "
                    "VALUES (%s,%s,%s,%s::jsonb,'draft',%s,%s::jsonb,%s) "
                    "ON CONFLICT (tenant_id, rule_key, version) DO NOTHING",
                    (tenant_id, rule_key, version, json.dumps(cfg, ensure_ascii=False),
                     source_ref, json.dumps(provenance, ensure_ascii=False), submitted_by),
                )
                inserted = cur.rowcount == 1
                # submit 动作：draft → pending_approval（同事务内推进）
                cur.execute(
                    "UPDATE billing_rules SET status='pending_approval' "
                    "WHERE tenant_id=%s AND rule_key=%s AND version=%s AND status='draft'",
                    (tenant_id, rule_key, version),
                )
        if inserted:
            break
    else:
        raise ErpError("INTERNAL", "规则版本登记并发冲突，请重试")
    audit(conn, tenant_id, "billing_rule_submitted", actor=submitted_by, target=rule_key,
          detail={"version": version, "fee_item": cfg["fee_item"],
                  "rate_source": cfg["rate_source"], "source_ref": source_ref})
    return {"rule_id": _rule_id_of(conn, tenant_id, rule_key, version), "rule_key": rule_key,
            "version": version, "status": "pending_approval"}


def _rule_id_of(conn: Any, tenant_id: str, rule_key: str, version: int) -> int:
    row = fetch_one(conn, "SELECT id FROM billing_rules WHERE tenant_id=%s AND rule_key=%s "
                          "AND version=%s", (tenant_id, rule_key, version))
    if row is None:
        raise ErpError("INTERNAL", "规则登记后回读失败")
    return int(row["id"])


def _intervals_overlap(a_from: date | None, a_to: date | None,
                       b_from: date | None, b_to: date | None) -> bool:
    """None = 开区间端点（±∞）。"""
    left_ok = a_from is None or b_to is None or a_from <= b_to
    right_ok = b_from is None or a_to is None or b_from <= a_to
    return left_ok and right_ok


def detect_conflicts(conn: Any, tenant_id: str, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """上线冲突检测（post-state：同 rule_key 旧版本按 succession 退役后的 active 集）。

    冲突两类：
      overlap — 同 fee_item 区间相交且无显式优先级（双方都有 priority 且不等才算处置）
      gap     — 同 fee_item 生效期存在空洞（相邻规则之间 B.from > A.to + 1 天）
    """
    cfg = candidate["config"] if isinstance(candidate["config"], dict) else \
        json.loads(candidate["config"])
    fee_item = str(cfg.get("fee_item") or "")
    actives = fetch_all(
        conn,
        "SELECT id, rule_key, version, config FROM billing_rules "
        "WHERE tenant_id=%s AND status='active' AND config->>'fee_item'=%s",
        (tenant_id, fee_item),
    )
    post_state = [r for r in actives if r["rule_key"] != candidate["rule_key"]]
    post_state.append(candidate)

    def bounds(r: dict[str, Any]) -> tuple[date | None, date | None, Decimal | None]:
        c = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
        prio = None if c.get("priority") is None else _dec(c["priority"], what="priority")
        return _cfg_date(c.get("effective_from"), what="effective_from"), \
            _cfg_date(c.get("effective_to"), what="effective_to"), prio

    conflicts: list[dict[str, Any]] = []
    for i in range(len(post_state)):
        for j in range(i + 1, len(post_state)):
            a, b = post_state[i], post_state[j]
            a_from, a_to, a_prio = bounds(a)
            b_from, b_to, b_prio = bounds(b)
            if _intervals_overlap(a_from, a_to, b_from, b_to):
                resolved = (a_prio is not None and b_prio is not None and a_prio != b_prio)
                if not resolved:
                    conflicts.append({
                        "type": "overlap",
                        "rule_key_a": a["rule_key"], "version_a": a["version"],
                        "rule_key_b": b["rule_key"], "version_b": b["version"],
                        "window_a": [str(a_from) if a_from else None, str(a_to) if a_to else None],
                        "window_b": [str(b_from) if b_from else None, str(b_to) if b_to else None],
                        "reason": "区间相交且无显式优先级（priority 需双方显式且不等）",
                    })
    ordered = sorted(post_state, key=lambda r: (bounds(r)[0] is not None, bounds(r)[0] or date.min))
    for a, b in zip(ordered, ordered[1:]):
        a_from, a_to, _ = bounds(a)
        b_from, b_to, _ = bounds(b)
        if a_to is None:
            break  # 前序已覆盖至 +∞，其后只剩 overlap 场景（上面已查）
        if b_from is None:
            continue  # b 从 -∞ 起，与 a 必然相交（overlap 已查）
        if b_from > a_to + timedelta(days=1):
            conflicts.append({
                "type": "gap",
                "rule_key_a": a["rule_key"], "version_a": a["version"],
                "rule_key_b": b["rule_key"], "version_b": b["version"],
                "hole": [str(a_to + timedelta(days=1)), str(b_from - timedelta(days=1))],
                "reason": "生效期存在覆盖空洞（生效期空洞未处置）",
            })
    return conflicts


def approve_billing_rule(conn: Any, *, settings: Settings, tenant_id: str,
                         rule_id: int, version: int, approved_by: str = "human") -> dict[str, Any]:
    """确认门：pending_approval → active（复用 proposal 确认语义）。

    - 冲突未处置 → RULE_CONFLICT 拒绝（规则保持 pending_approval）。
    - 不接受程序自审自批：approved_by 不得为空/billing-engine/system。
    - 同 rule_key 旧 active 版本随新版本上线自动退役（version succession）。
    """
    actor = str(approved_by or "").strip()
    if not actor or actor in ("billing-engine", "system"):
        raise ErpError("INVALID_ARGUMENT",
                       "不接受程序自审自批：approved_by 须为人工确认者标识",
                       detail={"approved_by": approved_by,
                               "forbidden": ["billing-engine", "system", ""]})
    row = fetch_one(conn, "SELECT * FROM billing_rules WHERE tenant_id=%s AND id=%s "
                          "AND version=%s", (tenant_id, rule_id, version))
    if row is None:
        raise ErpError("NOT_FOUND", "计费规则版本不存在",
                       detail={"rule_id": rule_id, "version": version})
    if row["status"] == "active":
        return {"ok": True, "status": "active", "rule_id": rule_id, "version": version,
                "idempotent_hit": True}
    if row["status"] != "pending_approval":
        raise ErpError("RULE_STATE", f"规则状态为 {row['status']}，仅 pending_approval 可审批",
                       detail={"rule_id": rule_id, "version": version, "status": row["status"]})

    conflicts = detect_conflicts(conn, tenant_id, row)
    if conflicts:
        raise ErpError("RULE_CONFLICT", "规则冲突未处置，上线被拦截",
                       detail={"rule_id": rule_id, "version": version, "conflicts": conflicts})

    # 覆盖缺失差集（design D9）：按 post-state 判定——候选版本上线、同 rule_key 旧版本
    # 随 approval 退役。任一登记在用的收费项在 post-state 仍无 active 规则 → 拒绝，
    # 不拿日志/告警放行。未启用登记（零登记行）的租户不受此闸影响（迁移过渡口径）。
    cfg_row = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
    missing_items = coverage_gap(
        conn, tenant_id,
        drop_rule_keys=frozenset({str(row["rule_key"])}),
        add_fee_item=str(cfg_row.get("fee_item") or ""),
    )
    if missing_items:
        raise ErpError(
            "FEE_COVERAGE_MISSING",
            f"收费项覆盖缺失，上线被拦截：{', '.join(missing_items)} 当前无 active 规则",
            detail={"stage": "rule_approval", "rule_id": rule_id, "version": version,
                    "fee_items": missing_items},
        )

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE billing_rules SET status='active', approved_by=%s, approved_at=now() "
                "WHERE tenant_id=%s AND id=%s AND version=%s AND status='pending_approval'",
                (actor, tenant_id, rule_id, version),
            )
            if cur.rowcount != 1:
                raise ErpError("RULE_STATE", "规则状态已变化，审批未生效")
            # version succession：同 rule_key 的其他 active 版本退役
            cur.execute(
                "UPDATE billing_rules SET status='retired' "
                "WHERE tenant_id=%s AND rule_key=%s AND status='active' "
                "AND NOT (id=%s AND version=%s)",
                (tenant_id, row["rule_key"], rule_id, version),
            )
    audit(conn, tenant_id, "billing_rule_approved", actor=actor, target=row["rule_key"],
          detail={"rule_id": rule_id, "version": version})
    return {"ok": True, "status": "active", "rule_id": rule_id, "version": version,
            "idempotent_hit": False}


def list_billing_rules(conn: Any, tenant_id: str, status: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT * FROM billing_rules WHERE tenant_id=%s ")
    params: list[Any] = [tenant_id]
    if status:
        sql += "AND status=%s "
        params.append(status)
    sql += "ORDER BY created_at, id"
    rows = fetch_all(conn, sql, tuple(params))
    out = []
    for r in rows:
        view = _rule_view(r)
        view["config"] = r["config"]
        out.append(view)
    return out


# ---------------------------------------------------------------------------
# 规则匹配（事件 / 期间快照共用）
# ---------------------------------------------------------------------------

def _window_covers(cfg: dict[str, Any], d: date) -> bool:
    f = _cfg_date(cfg.get("effective_from"), what="effective_from")
    t = _cfg_date(cfg.get("effective_to"), what="effective_to")
    return (f is None or f <= d) and (t is None or t >= d)


def _pick_top_by_item(rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """同 fee_item 多条命中时取显式优先级最高（数值最小）者，其余记为被取代。"""
    by_item: dict[str, list[dict[str, Any]]] = {}
    for r in rules:
        cfg = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
        by_item.setdefault(str(cfg.get("fee_item")), []).append(r)
    chosen: list[dict[str, Any]] = []
    superseded: list[int] = []
    for _item, rs in by_item.items():
        def prio(r: dict[str, Any]) -> tuple[Decimal, int, int]:
            c = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
            p = Decimal("Infinity") if c.get("priority") is None \
                else _dec(c["priority"], what="priority")
            return (p, -int(r["version"]), int(r["id"]))
        ordered = sorted(rs, key=prio)
        chosen.append(ordered[0])
        superseded.extend(int(r["id"]) for r in ordered[1:])
    return chosen, superseded


def _match_event_rules(active_rules: list[dict[str, Any]], *, kind: str,
                       occurred: date) -> list[dict[str, Any]]:
    matched = []
    for r in active_rules:
        cfg = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
        if str(cfg.get("event_kind")) == kind and _window_covers(cfg, occurred):
            matched.append(r)
    return matched


# ---------------------------------------------------------------------------
# 收费项登记 + 覆盖缺失差集（design D9）
#
# 口径：「登记在用集合 − active 规则集合」非空即阻断，三个拦截点共用同一组差集：
#   rule_approval  — 审批上线时（post-state：候选版本上线、同 rule_key 旧版本退役）
#   event_billing  — 事件计费时（该事件类型本应命中的在用收费项都须有覆盖该日的规则，
#                    且计费命中的收费项必须已登记）
#   period_billing — 日终快照计费时（整期间判定，不生成部分批次）
# 迁移过渡：租户一条登记都没有 → 视为「尚未启用登记」，不做覆盖阻断（登记是显式
# 运维动作，register-fee-item）。一旦有登记，差集判定对全部拦截点生效，不放行。
# ---------------------------------------------------------------------------

def register_fee_item(conn: Any, *, tenant_id: str, fee_item: str, event_kind: str,
                      source_ref: str | None = None, registered_by: str = "ops",
                      status: str = "in_use") -> dict[str, Any]:
    """登记/改登记一个收费项（运维动作；MCP 工具面不扩这一件）。

    status='in_use' 表示在用（进入覆盖差集）；'retired' 表示停用（退出差集）。
    幂等：同 (tenant, fee_item, event_kind) 重复登记只更新状态与出处。
    """
    fee_item = str(fee_item or "").strip()
    event_kind = str(event_kind or "").strip()
    if not fee_item:
        raise ErpError("INVALID_ARGUMENT", "fee_item 不能为空")
    if not event_kind:
        raise ErpError("INVALID_ARGUMENT", "event_kind 不能为空（收费项由哪种事件触发）")
    status = str(status or "in_use").strip()
    if status not in ("in_use", "retired"):
        raise ErpError("INVALID_ARGUMENT", "status 须为 in_use/retired",
                       detail={"status": status})
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO billing_fee_items(tenant_id, fee_item, event_kind, status, "
                "source_ref, registered_by, retired_at) "
                "VALUES (%s,%s,%s,%s,%s,%s, CASE WHEN %s='retired' THEN now() END) "
                "ON CONFLICT (tenant_id, fee_item, event_kind) DO UPDATE SET "
                "status=excluded.status, source_ref=excluded.source_ref, "
                "registered_by=excluded.registered_by, retired_at=excluded.retired_at",
                (tenant_id, fee_item, event_kind, status, source_ref, registered_by, status),
            )
    audit(conn, tenant_id, "fee_item_registered", actor=registered_by, target=fee_item,
          detail={"event_kind": event_kind, "status": status, "source_ref": source_ref})
    return {"fee_item": fee_item, "event_kind": event_kind, "status": status}


def list_fee_items(conn: Any, tenant_id: str, status: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT fee_item, event_kind, status, source_ref, registered_by, "
           "created_at, retired_at FROM billing_fee_items WHERE tenant_id=%s ")
    params: list[Any] = [tenant_id]
    if status:
        sql += "AND status=%s "
        params.append(status)
    sql += "ORDER BY fee_item, event_kind"
    rows = fetch_all(conn, sql, tuple(params))
    for r in rows:
        for k in ("created_at", "retired_at"):
            if r.get(k) is not None:
                r[k] = str(r[k])
    return rows


def _registry_adopted(conn: Any, tenant_id: str) -> bool:
    """该租户是否已启用收费项登记（有任意一行，含 retired）。"""
    return fetch_one(conn, "SELECT 1 AS x FROM billing_fee_items WHERE tenant_id=%s LIMIT 1",
                     (tenant_id,)) is not None


def _in_use_fee_items(conn: Any, tenant_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        "SELECT fee_item, event_kind FROM billing_fee_items "
        "WHERE tenant_id=%s AND status='in_use'",
        (tenant_id,),
    )


def _covered_fee_items(conn: Any, tenant_id: str, *,
                       drop_rule_keys: frozenset[str] = frozenset(),
                       add_fee_item: str | None = None) -> set[str]:
    """active 规则覆盖到的收费项集合（可模拟 post-state：退役某些 rule_key + 新增候选）。"""
    actives = fetch_all(conn, "SELECT rule_key, config FROM billing_rules "
                              "WHERE tenant_id=%s AND status='active'", (tenant_id,))
    covered: set[str] = set()
    for r in actives:
        if r["rule_key"] in drop_rule_keys:
            continue
        cfg = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
        item = str(cfg.get("fee_item") or "").strip()
        if item:
            covered.add(item)
    if add_fee_item:
        covered.add(add_fee_item)
    return covered


def coverage_gap(conn: Any, tenant_id: str, *,
                 drop_rule_keys: frozenset[str] = frozenset(),
                 add_fee_item: str | None = None) -> list[str]:
    """「登记在用集合 − active 规则集合」；空列表 = 无缺失（或尚未启用登记）。

    已在审批管道里的收费项（存在 pending_approval 规则）不计入缺口：它本身就是待上线
    的候选，把它当缺口会让「两个收费项依次上线」永久死锁——批准 A 时 B 未上线，
    批准 B 时 A 又成缺口。真正要拦的是「停旧未上新」：该收费项没有任何 pending 候选，
    仍会被点名。
    """
    if not _registry_adopted(conn, tenant_id):
        return []
    in_use = {str(r["fee_item"]) for r in _in_use_fee_items(conn, tenant_id)}
    pipeline = _pending_fee_items(conn, tenant_id)
    covered = _covered_fee_items(conn, tenant_id, drop_rule_keys=drop_rule_keys,
                                 add_fee_item=add_fee_item)
    return sorted((in_use - pipeline) - covered)


def _pending_fee_items(conn: Any, tenant_id: str) -> set[str]:
    """审批管道中的收费项（有 pending_approval 规则）。"""
    rows = fetch_all(
        conn,
        "SELECT config FROM billing_rules WHERE tenant_id=%s AND status='pending_approval'",
        (tenant_id,),
    )
    out: set[str] = set()
    for r in rows:
        cfg = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
        item = str(cfg.get("fee_item") or "").strip()
        if item:
            out.add(item)
    return out


def _event_coverage_gap(conn: Any, tenant_id: str, *, kind: str, occurred: date,
                        matched_fee_items: set[str]) -> tuple[list[str], list[str]]:
    """事件计费覆盖判定：返回 (本应命中却无 active 规则的项, 命中但未登记的项)。"""
    if not _registry_adopted(conn, tenant_id):
        return [], []
    in_use = _in_use_fee_items(conn, tenant_id)
    registered = {str(r["fee_item"]) for r in in_use}
    expected = {str(r["fee_item"]) for r in in_use if str(r["event_kind"]) == kind}
    actives = fetch_all(conn, "SELECT config FROM billing_rules WHERE tenant_id=%s "
                              "AND status='active'", (tenant_id,))
    covered: set[str] = set()
    for r in actives:
        cfg = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
        if str(cfg.get("event_kind")) == kind and _window_covers(cfg, occurred):
            covered.add(str(cfg.get("fee_item") or ""))
    missing_rule = sorted(expected - covered)
    unregistered = sorted(matched_fee_items - registered)
    return missing_rule, unregistered


# ---------------------------------------------------------------------------
# 事件计费 + 日终快照计费
# ---------------------------------------------------------------------------

def _validate_event(event: Any) -> tuple[str, str, date, dict[str, Any], dict[str, Any]]:
    if not isinstance(event, dict):
        raise ErpError("INVALID_ARGUMENT", "event 须为对象")
    stable = str(event.get("stable_id") or "").strip()
    kind = str(event.get("kind") or "").strip()
    occurred_raw = str(event.get("occurred_at") or "").strip()
    measures = event.get("measures")
    if not stable:
        raise ErpError("INVALID_ARGUMENT", "event.stable_id 不能为空")
    if not kind:
        raise ErpError("INVALID_ARGUMENT", "event.kind 不能为空")
    if not occurred_raw:
        raise ErpError("INVALID_ARGUMENT", "event.occurred_at 不能为空")
    if not isinstance(measures, dict) or not measures:
        raise ErpError("INVALID_ARGUMENT", "event.measures 须为非空对象（计费量纲）")
    refs = event.get("refs") if isinstance(event.get("refs"), dict) else {}
    return stable, kind, _parse_date(occurred_raw, what="event.occurred_at"), measures, refs


def _fee_view(row: dict[str, Any], journal_proposal_id: Any = None) -> dict[str, Any]:
    """费用明细视图：原币 / 本位币 / 汇率四要素一并返回（design D7 可独立审计）。

    `amount` 保留为兼容列（= 计价币种金额 = original_amount）；`original_amount` /
    `base_amount` / `fx_*` 对 0005 之前的存量行为 None（不补造折算口径）。

    `refs` 是**单据引用**（计费时 `event["refs"]` 原样落到 `provenance.document_refs`）。
    列表读面也必须带出来：缺了它，任何按单据/承运商归集都得逐条调 `get_fee_detail`，
    而调用方自己维护一份「费用→单据」映射就是第二份事实源（change:
    add-carrier-freight-dimension D4）。缺失回 `{}`，不省略字段。
    """
    prov = row["provenance"] if isinstance(row["provenance"], dict) else {}
    jp = prov.get("journal") or {}
    refs = prov.get("document_refs")
    if not isinstance(refs, dict):
        refs = {}

    def _s(key: str) -> str | None:
        value = row.get(key)
        return None if value is None else str(value)

    return {
        "fee_id": row["id"],
        "stable_id": row["stable_id"],
        "event_stable_id": row["event_stable_id"],
        "fee_item": row["fee_item"],
        "amount": str(row["amount"]),
        "currency": row["currency"],
        "original_amount": _s("original_amount"),
        "base_amount": _s("base_amount"),
        "fx_rate": _s("fx_rate"),
        "fx_as_of": _s("fx_as_of"),
        "fx_source": row.get("fx_source"),
        "rule_id": row["rule_id"],
        "rule_version": row["rule_version"],
        "status": row["status"],
        "reversal_of": row["reversal_of"],
        "created_at": str(row["created_at"]),
        "refs": refs,
        "journal_proposal_id": journal_proposal_id if journal_proposal_id is not None
                               else jp.get("proposal_id"),
    }


def _resolve_fee_fx(conn: Any, *, settings: Settings, cfg: dict[str, Any],
                    amount: Decimal, priced_on: date) -> dict[str, Any]:
    """按 fx_facts 的登记事实折算一笔费用；返回折算四要素 + 本位币金额。

    **调用方必须在任何写入之前先解析全部费用**：缺出处 / 无登记事实 → FX_UNSOURCED，
    fee_details 零行、journal_entries 零分录（不留半批）。

    - 出处取 `config.fx_source`，缺省回落 `Settings.fx_source`（ERP_FX_SOURCE）；
      两边都没有 → 拒绝（不猜默认汇率）。
    - 「与登记不符」的第二道闸在账本层：折算出的 rate/as_of/source 进入分录行后，
      `ledger._normalize_lines` 会重新按 fx_facts 核对，不一致同样 FX_UNSOURCED。
    """
    currency = _fee_currency(cfg, settings=settings)
    if currency == settings.base_currency:
        base = amount.quantize(Q6, rounding=ROUND_HALF_UP)
        return {"currency": currency, "original_amount": amount, "base_amount": base,
                "fx_rate": None, "fx_as_of": None, "fx_source": None}
    source = str(cfg.get("fx_source") or settings.fx_source or "").strip()
    as_of = str(priced_on)
    if not source:
        raise ErpError(
            "FX_UNSOURCED",
            f"非本位币费用 {currency} 无汇率出处（规则未指定 fx_source，且未配置默认汇率源）",
            detail={"currency": currency, "as_of": as_of, "source": None,
                    "fee_item": cfg.get("fee_item"), "reason": "no_source"},
        )
    rate = resolve_rate(conn, currency, as_of, source, settings=settings)
    if rate is None:
        raise ErpError(
            "FX_UNSOURCED",
            f"非本位币费用 {currency} 在 {as_of} 没有登记汇率（source={source}）；拒绝入账",
            detail={"currency": currency, "as_of": as_of, "source": source,
                    "fee_item": cfg.get("fee_item"), "reason": "no_registered_rate"},
        )
    base = (amount * rate).quantize(Q6, rounding=ROUND_HALF_UP)
    return {"currency": currency, "original_amount": amount, "base_amount": base,
            "fx_rate": rate, "fx_as_of": as_of, "fx_source": source}


def _price_rule(conn: Any, *, settings: Settings, rule: dict[str, Any],
                measures: dict[str, Any], priced_on: date,
                snapshot: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    """计价 + 折算（任何一步失败都在写入之前抛出；事件与期间快照共用）。"""
    cfg = rule["config"] if isinstance(rule["config"], dict) else json.loads(rule["config"])
    amount, breakdown = compute_fee_with_breakdown(cfg, measures)
    fx = _resolve_fee_fx(conn, settings=settings, cfg=cfg, amount=amount, priced_on=priced_on)
    snap = dict(snapshot)
    snap.update({
        "rule_config": cfg,
        "breakdown": breakdown,
        "fx": {
            "original_amount": str(fx["original_amount"]),
            "base_amount": str(fx["base_amount"]),
            "rate": None if fx["fx_rate"] is None else str(fx["fx_rate"]),
            "as_of": fx["fx_as_of"],
            "source": fx["fx_source"],
        },
    })
    return {
        "rule_id": int(rule["id"]),
        "rule_version": int(rule["version"]),
        "fee_item": str(cfg["fee_item"]),
        "amount": amount,
        "original_amount": fx["original_amount"],
        "base_amount": fx["base_amount"],
        "currency": fx["currency"],
        "fx_rate": None if fx["fx_rate"] is None else str(fx["fx_rate"]),
        "fx_as_of": fx["fx_as_of"],
        "fx_source": fx["fx_source"],
        "fee_type": str(cfg.get("fee_type") or "revenue"),
        "snapshot": snap,
        "provenance": provenance,
    }


def _record_billed(path: str, tenant_id: str, rules_amounts: list[dict[str, Any]]) -> None:
    """计费笔数按币种落指标（币种是账务口径，不是客户标识）。"""
    per_currency: dict[str, int] = defaultdict(int)
    for ra in rules_amounts:
        per_currency[str(ra["currency"])] += 1
    for currency, count in per_currency.items():
        obs.fees_billed(path, currency=currency, count=count, tenant_id=tenant_id)


def _fee_fx_key(fee: dict[str, Any]) -> tuple[str, str, str, str]:
    """分录分组键：同币种同汇率事实的费用才可汇总成一行。"""
    return (str(fee["currency"]),
            "" if fee.get("fx_rate") is None else str(fee["fx_rate"]),
            "" if fee.get("fx_as_of") is None else str(fee["fx_as_of"]),
            "" if fee.get("fx_source") is None else str(fee["fx_source"]))


def _compose_and_submit_journal(
    conn: Any, *, settings: Settings, tenant_id: str, fees: list[dict[str, Any]],
    journal_stable: str, source_ref: str | None,
) -> tuple[int | None, str | None]:
    """把费用组成机器车道分录 proposal（借 1300 / 贷 4000；费用类借 5100 / 贷 1300）。

    按 (currency, fx_rate, fx_as_of, fx_source) 分组：非本位币费用不折成一笔「本位币
    汇总」，而是按原币金额 + 汇率四要素成行，让分录自己解释金额（design D7）。
    同组借贷两侧同额 → 折算后的 amount_base 相等，借贷必平仍由 DB 延迟约束在 COMMIT
    判定。汇率已在调用方预解析，本函数 **不做任何静默跳过**。
    返回 (proposal_id, note)。
    """
    groups: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: {"revenue": Decimal(0), "expense": Decimal(0)})
    for f in fees:
        bucket = "revenue" if f["fee_type"] == "revenue" else "expense"
        groups[_fee_fx_key(f)][bucket] += Decimal(str(f["amount"]))

    lines: list[dict[str, Any]] = []
    for key in sorted(groups):
        currency, rate, as_of, source = key
        rev = groups[key]["revenue"]
        exp = groups[key]["expense"]
        common: dict[str, Any] = {"currency": currency}
        if currency != settings.base_currency:
            common.update({"fx_rate": rate, "fx_as_of": as_of, "fx_source": source})
        if rev > 0:
            lines.append({"account_code": "1300", "direction": "debit",
                          "amount": str(rev.quantize(Q6)), **common})
            lines.append({"account_code": "4000", "direction": "credit",
                          "amount": str(rev.quantize(Q6)), **common})
        if exp > 0:
            lines.append({"account_code": "5100", "direction": "debit",
                          "amount": str(exp.quantize(Q6)), **common})
            lines.append({"account_code": "1300", "direction": "credit",
                          "amount": str(exp.quantize(Q6)), **common})
    if not lines:
        return None, "费用金额为零，未生成分录 proposal"
    prop = submit_proposal(
        conn, settings=settings, tenant_id=tenant_id, stable_id=journal_stable,
        lines=lines, source_ref=source_ref, kind="journal",
        submitted_by="billing-engine",
    )
    return prop["proposal_id"], None


def _insert_fee_rows(conn: Any, *, tenant_id: str, rules_amounts: list[dict[str, Any]],
                     provenance_extra: dict[str, Any]) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            for ra in rules_amounts:
                prov = dict(ra["provenance"])
                prov.update(provenance_extra)
                cur.execute(
                    "INSERT INTO fee_details(tenant_id, rule_id, rule_version, stable_id, "
                    "event_stable_id, fee_item, snapshot, amount, original_amount, base_amount, "
                    "currency, fx_rate, fx_as_of, fx_source, provenance, status) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'normal') "
                    "ON CONFLICT (tenant_id, stable_id) DO NOTHING",
                    (tenant_id, ra["rule_id"], ra["rule_version"], ra["stable_id"],
                     ra["event_stable_id"], ra["fee_item"],
                     json.dumps(ra["snapshot"], ensure_ascii=False), str(ra["amount"]),
                     str(ra["original_amount"]), str(ra["base_amount"]),
                     ra["currency"], ra["fx_rate"], ra["fx_as_of"], ra["fx_source"],
                     json.dumps(prov, ensure_ascii=False)),
                )


def bill_event(conn: Any, *, settings: Settings, tenant_id: str, event: dict[str, Any],
               source_ref: str | None = None) -> dict[str, Any]:
    """事件计费：按 active 规则匹配计价 → fee_details（append-only）+ 机器车道 proposal。

    幂等：同 event.stable_id 重推返回首次结果（UNIQUE(tenant_id, stable_id) 兜底）。
    计费快照锁定规则版本 + 基础数据，可重算一致（get_fee_detail.snapshot_replay）。
    覆盖缺失（登记在用项无 active 规则 / 命中未登记项）在写入前阻断并点名收费项。
    失败按封闭错误码归因落指标（erp_billing_failures_total{reason}）。
    """
    try:
        return _bill_event(conn, settings=settings, tenant_id=tenant_id, event=event,
                           source_ref=source_ref)
    except ErpError as exc:
        obs.billing_failed(exc.code, path="event", tenant_id=tenant_id,
                           detail={"stage": exc.detail.get("stage"),
                                   "fee_items": exc.detail.get("fee_items"),
                                   "unregistered_fee_items":
                                       exc.detail.get("unregistered_fee_items"),
                                   "currency": exc.detail.get("currency"),
                                   "as_of": exc.detail.get("as_of")})
        raise


def _bill_event(conn: Any, *, settings: Settings, tenant_id: str, event: dict[str, Any],
                source_ref: str | None) -> dict[str, Any]:
    stable, kind, occurred, measures, refs = _validate_event(event)
    existing = fetch_all(
        conn, "SELECT * FROM fee_details WHERE tenant_id=%s AND event_stable_id=%s ORDER BY id",
        (tenant_id, stable),
    )
    if existing:
        return {"fees": [_fee_view(r) for r in existing], "stable_id": stable,
                "duplicate": True}

    actives = fetch_all(conn, "SELECT * FROM billing_rules WHERE tenant_id=%s "
                              "AND status='active'", (tenant_id,))
    matched = _match_event_rules(actives, kind=kind, occurred=occurred)

    # 覆盖缺失阻断（design D9）：先于任何写入判定，不生成部分费用明细。
    matched_items = {str(cfg.get("fee_item") or "") for cfg in
                     (r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
                      for r in matched)}
    missing_rule, unregistered = _event_coverage_gap(
        conn, tenant_id, kind=kind, occurred=occurred, matched_fee_items=matched_items)
    if missing_rule or unregistered:
        named = sorted(set(missing_rule) | set(unregistered))
        raise ErpError(
            "FEE_COVERAGE_MISSING",
            f"事件计费被拒：收费项覆盖缺失（{'、'.join(named)}）",
            detail={"stage": "event_billing", "event_kind": kind, "as_of": str(occurred),
                    "fee_items": missing_rule, "unregistered_fee_items": unregistered},
        )

    chosen, superseded = _pick_top_by_item(matched)

    rules_amounts: list[dict[str, Any]] = []
    for r in chosen:
        cfg = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
        ra = _price_rule(
            conn, settings=settings, rule=r, measures=measures, priced_on=occurred,
            snapshot={"event": event, "measures_used": measures},
            provenance={
                "event": {"stable_id": stable, "kind": kind,
                          "occurred_at": str(event.get("occurred_at")), "measures": measures},
                "document_refs": refs,
                "fee_item": str(cfg["fee_item"]),
                "rule": {"rule_id": int(r["id"]), "version": int(r["version"]),
                         "rule_key": r["rule_key"], "rate_source": cfg.get("rate_source"),
                         "mode": cfg.get("mode")},
                "superseded_rule_ids": superseded,
            },
        )
        ra["stable_id"] = f"{stable}:{r['rule_key']}"
        ra["event_stable_id"] = stable
        rules_amounts.append(ra)

    journal_proposal_id: int | None = None
    journal_note: str | None = None
    if rules_amounts:
        journal_proposal_id, journal_note = _compose_and_submit_journal(
            conn, settings=settings, tenant_id=tenant_id, fees=rules_amounts,
            journal_stable=f"{stable}:fee-journal",
            source_ref=source_ref or f"billing:{stable}",
        )
        for ra in rules_amounts:
            ra["provenance"]["journal"] = {"proposal_id": journal_proposal_id,
                                           "stable_id": f"{stable}:fee-journal"}
        _insert_fee_rows(conn, tenant_id=tenant_id, rules_amounts=rules_amounts,
                         provenance_extra={})
        audit(conn, tenant_id, "fee_billed", actor="billing-engine", target=stable,
              detail={"fees": len(rules_amounts),
                      "journal_proposal_id": journal_proposal_id})
        _record_billed("event", tenant_id, rules_amounts)

    rows = fetch_all(
        conn, "SELECT * FROM fee_details WHERE tenant_id=%s AND event_stable_id=%s ORDER BY id",
        (tenant_id, stable),
    )
    result: dict[str, Any] = {"fees": [_fee_view(r) for r in rows], "stable_id": stable,
                              "duplicate": False}
    if journal_note:
        result["journal_note"] = journal_note
    return result


def _validate_snapshot_item(snapshot: Any, *, what: str) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ErpError("INVALID_ARGUMENT", f"{what} 须为对象")
    fee_item = str(snapshot.get("fee_item") or "").strip()
    as_of_raw = str(snapshot.get("as_of") or "").strip()
    measures = snapshot.get("measures")
    if not fee_item:
        raise ErpError("INVALID_ARGUMENT", f"{what}.fee_item 不能为空")
    if not as_of_raw:
        raise ErpError("INVALID_ARGUMENT", f"{what}.as_of 不能为空")
    if not isinstance(measures, dict) or not measures:
        raise ErpError("INVALID_ARGUMENT", f"{what}.measures 须为非空对象")
    refs = snapshot.get("refs") if isinstance(snapshot.get("refs"), dict) else {}
    return {"fee_item": fee_item,
            "as_of": _parse_date(as_of_raw, what=f"{what}.as_of"),
            "measures": measures, "refs": refs, "raw": snapshot}


def bill_period_snapshot(conn: Any, *, settings: Settings, tenant_id: str, period: str,
                         snapshot: dict[str, Any], source_ref: str | None = None) -> dict[str, Any]:
    """单个日终快照计费（库 API 兼容入口）；批量语义见 bill_period_snapshots。"""
    return bill_period_snapshots(conn, settings=settings, tenant_id=tenant_id, period=period,
                                 snapshots=[snapshot], source_ref=source_ref)


def bill_period_snapshots(conn: Any, *, settings: Settings, tenant_id: str, period: str,
                          snapshots: list[dict[str, Any]] | dict[str, Any],
                          source_ref: str | None = None) -> dict[str, Any]:
    """日终快照计费（批量、期间幂等、覆盖门禁、一条批次分录）。

    snapshot 每项：{fee_item, as_of, measures: {...}, [refs]}
    幂等键 = (tenant, period)：event_stable_id = `bill-period:<period>`，
    明细 stable_id = `bill-period:<period>:<rule_key>`；同期间重跑返回首次结果
    （duplicate=True），不产生重复费用（UNIQUE(tenant_id, stable_id) 兜底）。

    覆盖缺失（design D9）：整期间判定——登记在用集合缺 active 规则、或请求快照含
    未登记收费项 → 拒绝且 **不生成部分批次**。
    """
    try:
        return _bill_period_snapshots(conn, settings=settings, tenant_id=tenant_id,
                                     period=period, snapshots=snapshots,
                                     source_ref=source_ref)
    except ErpError as exc:
        obs.billing_failed(exc.code, path="period", tenant_id=tenant_id,
                           detail={"stage": exc.detail.get("stage"),
                                   "fee_items": exc.detail.get("fee_items"),
                                   "unregistered_fee_items":
                                       exc.detail.get("unregistered_fee_items"),
                                   "currency": exc.detail.get("currency"),
                                   "as_of": exc.detail.get("as_of")})
        raise


def _bill_period_snapshots(conn: Any, *, settings: Settings, tenant_id: str, period: str,
                           snapshots: list[dict[str, Any]] | dict[str, Any],
                           source_ref: str | None) -> dict[str, Any]:
    period = str(period or "").strip()
    if not period:
        raise ErpError("INVALID_ARGUMENT", "period 不能为空")
    if isinstance(snapshots, dict):
        snapshots = [snapshots]
    if not isinstance(snapshots, list) or not snapshots:
        raise ErpError("INVALID_ARGUMENT", "期间快照计费须提供非空 snapshots 数组")
    items = [_validate_snapshot_item(s, what=f"snapshots[{i}]")
             for i, s in enumerate(snapshots)]
    seen: set[str] = set()
    for it in items:
        if it["fee_item"] in seen:
            raise ErpError("INVALID_ARGUMENT",
                           f"同一期间快照中收费项重复：{it['fee_item']}（每期每项只能报一份日终快照）",
                           detail={"fee_item": it["fee_item"], "period": period})
        seen.add(it["fee_item"])

    event_stable = f"bill-period:{period}"
    existing = fetch_all(
        conn, "SELECT * FROM fee_details WHERE tenant_id=%s AND event_stable_id=%s ORDER BY id",
        (tenant_id, event_stable),
    )
    if existing:
        return {"fees": [_fee_view(r) for r in existing], "period": period,
                "stable_id": event_stable, "duplicate": True}

    # 覆盖门禁（整期间判定，先于任何写入）
    missing = coverage_gap(conn, tenant_id)
    unregistered: list[str] = []
    if _registry_adopted(conn, tenant_id):
        registered = {str(r["fee_item"]) for r in _in_use_fee_items(conn, tenant_id)}
        unregistered = sorted(seen - registered)
    if missing or unregistered:
        named = sorted(set(missing) | set(unregistered))
        raise ErpError(
            "FEE_COVERAGE_MISSING",
            f"日终快照计费被拒：期间 {period} 存在覆盖缺失的收费项（{'、'.join(named)}）",
            detail={"stage": "period_billing", "period": period,
                    "fee_items": missing, "unregistered_fee_items": unregistered},
        )

    actives = fetch_all(conn, "SELECT * FROM billing_rules WHERE tenant_id=%s "
                              "AND status='active'", (tenant_id,))
    rules_amounts: list[dict[str, Any]] = []
    unpriced: list[str] = []
    for it in items:
        candidates: list[dict[str, Any]] = []
        for r in actives:
            cfg = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
            if str(cfg.get("fee_item")) == it["fee_item"] and _window_covers(cfg, it["as_of"]):
                candidates.append(r)
        chosen, superseded = _pick_top_by_item(candidates)
        if not chosen:
            # 调用方在快照里点名要对这个收费项计费，它却没有 active 规则：
            # 必须拒绝并点名，不能交一张空账。登记表未启用（迁移过渡期）时
            # coverage_gap 看不出这种缺失，只有这里能拦住。
            unpriced.append(it["fee_item"])
            continue
        for r in chosen:
            cfg = r["config"] if isinstance(r["config"], dict) else json.loads(r["config"])
            ra = _price_rule(
                conn, settings=settings, rule=r, measures=it["measures"],
                priced_on=it["as_of"],
                snapshot={"period": period, "snapshot": it["raw"],
                          "measures_used": it["measures"]},
                provenance={
                    "event": {"period": period, "as_of": str(it["as_of"]),
                              "measures": it["measures"]},
                    "document_refs": it["refs"],
                    "fee_item": str(cfg["fee_item"]),
                    "rule": {"rule_id": int(r["id"]), "version": int(r["version"]),
                             "rule_key": r["rule_key"], "rate_source": cfg.get("rate_source"),
                             "mode": cfg.get("mode")},
                    "superseded_rule_ids": superseded,
                },
            )
            ra["stable_id"] = f"{event_stable}:{r['rule_key']}"
            ra["event_stable_id"] = event_stable
            rules_amounts.append(ra)

    if unpriced:
        # 先于任何写入：一条都不落，避免「部分批次」。
        named = sorted(set(unpriced))
        raise ErpError(
            "FEE_COVERAGE_MISSING",
            f"日终快照计费被拒：期间 {period} 请求计费的收费项没有 active 规则"
            f"（{'、'.join(named)}）",
            detail={"stage": "period_billing", "period": period,
                    "fee_items": named, "unregistered_fee_items": []},
        )

    journal_proposal_id: int | None = None
    if rules_amounts:
        journal_proposal_id, _note = _compose_and_submit_journal(
            conn, settings=settings, tenant_id=tenant_id, fees=rules_amounts,
            journal_stable=f"{event_stable}:fee-journal",
            source_ref=source_ref or f"billing:{event_stable}",
        )
        for ra in rules_amounts:
            ra["provenance"]["journal"] = {"proposal_id": journal_proposal_id,
                                           "stable_id": f"{event_stable}:fee-journal"}
        _insert_fee_rows(conn, tenant_id=tenant_id, rules_amounts=rules_amounts,
                         provenance_extra={})
        audit(conn, tenant_id, "fee_billed", actor="billing-engine", target=event_stable,
              detail={"period": period, "fees": len(rules_amounts),
                      "journal_proposal_id": journal_proposal_id})
        _record_billed("period", tenant_id, rules_amounts)

    rows = fetch_all(
        conn, "SELECT * FROM fee_details WHERE tenant_id=%s AND event_stable_id=%s ORDER BY id",
        (tenant_id, event_stable),
    )
    return {"fees": [_fee_view(r) for r in rows], "period": period,
            "stable_id": event_stable, "duplicate": False}


# ---------------------------------------------------------------------------
# 红冲 + 四层溯源
# ---------------------------------------------------------------------------

def reverse_fee(conn: Any, *, settings: Settings, tenant_id: str, fee_id: int,
                reason: str, reversed_by: str = "human") -> dict[str, Any]:
    """红冲：生成反向明细行（status='reversed' + reversal_of），原行原样保留。

    幂等：已有反向行时返回首次结果。账本侧：原费用分录已落账 → 组冲正 proposal
    （kind=reversal，走既有确认车道）；尚未落账 → 仅红冲费用行并如实注明。
    """
    if not str(reason or "").strip():
        raise ErpError("INVALID_ARGUMENT", "红冲须给出 reason")
    fee = fetch_one(conn, "SELECT * FROM fee_details WHERE tenant_id=%s AND id=%s",
                    (tenant_id, fee_id))
    if fee is None:
        raise ErpError("NOT_FOUND", "费用明细不存在", detail={"fee_id": fee_id})
    prior = fetch_one(
        conn, "SELECT * FROM fee_details WHERE tenant_id=%s AND reversal_of=%s "
              "AND status='reversed' ORDER BY id LIMIT 1", (tenant_id, fee_id))
    if prior is not None:
        jp = prior["provenance"].get("journal") if isinstance(prior["provenance"], dict) else {}
        return {"reversal_fee_id": prior["id"],
                "journal_proposal_id": jp.get("proposal_id") if isinstance(jp, dict) else None,
                "idempotent_hit": True}

    fee_prov = fee["provenance"] if isinstance(fee["provenance"], dict) else {}
    rev_prov = dict(fee_prov)
    rev_prov["reversal_of_fee_id"] = int(fee_id)
    rev_prov["reversal_reason"] = str(reason)
    rev_prov["reversed_by"] = reversed_by
    rev_stable = f"{fee['stable_id']}:reversal"
    rev_snapshot = dict(fee["snapshot"]) if isinstance(fee["snapshot"], dict) else {}
    rev_snapshot["reversal"] = {"reason": str(reason), "reversed_by": reversed_by,
                                "reverses_fee_id": int(fee_id)}
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fee_details(tenant_id, rule_id, rule_version, stable_id, "
                "event_stable_id, fee_item, snapshot, amount, original_amount, base_amount, "
                "currency, fx_rate, fx_as_of, fx_source, provenance, status, reversal_of) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
                "'reversed',%s) ON CONFLICT (tenant_id, stable_id) DO NOTHING",
                (tenant_id, fee["rule_id"], fee["rule_version"], rev_stable,
                 fee["event_stable_id"], fee["fee_item"],
                 json.dumps(rev_snapshot, ensure_ascii=False), str(fee["amount"]),
                 None if fee.get("original_amount") is None else str(fee["original_amount"]),
                 None if fee.get("base_amount") is None else str(fee["base_amount"]),
                 fee["currency"],
                 None if fee.get("fx_rate") is None else str(fee["fx_rate"]),
                 fee.get("fx_as_of"), fee.get("fx_source"),
                 json.dumps(rev_prov, ensure_ascii=False), fee_id),
            )
    rev_row = fetch_one(conn, "SELECT * FROM fee_details WHERE tenant_id=%s AND stable_id=%s",
                        (tenant_id, rev_stable))

    journal_proposal_id: int | None = None
    journal_note: str | None = None
    jp = fee_prov.get("journal") if isinstance(fee_prov.get("journal"), dict) else {}
    orig_journal_stable = jp.get("stable_id")
    if orig_journal_stable:
        posted = fetch_one(
            conn, "SELECT txn_id FROM journal_entries WHERE tenant_id=%s AND stable_id=%s "
                  "LIMIT 1", (tenant_id, orig_journal_stable))
        if posted is not None:
            prop = submit_proposal(
                conn, settings=settings, tenant_id=tenant_id,
                stable_id=f"{fee['stable_id']}:reversal-journal",
                lines=[], kind="reversal", reversal_of=posted["txn_id"],
                source_ref=f"billing-reversal:{fee['stable_id']}",
                submitted_by="billing-engine",
            )
            journal_proposal_id = prop["proposal_id"]
        else:
            journal_note = "原费用分录尚未落账（proposal 未确认）；红冲仅记费用行，账本侧请拒绝原 proposal"
    else:
        journal_note = "原费用无关联分录 proposal；红冲仅记费用行"
    audit(conn, tenant_id, "fee_reversed", actor=reversed_by, target=fee["stable_id"],
          detail={"reversal_fee_id": rev_row["id"], "reason": str(reason),
                  "journal_proposal_id": journal_proposal_id})
    result: dict[str, Any] = {"reversal_fee_id": rev_row["id"],
                              "journal_proposal_id": journal_proposal_id,
                              "idempotent_hit": False}
    if journal_note:
        result["journal_note"] = journal_note
    return result


def list_fees(conn: Any, tenant_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        conn, "SELECT * FROM fee_details WHERE tenant_id=%s ORDER BY created_at, id",
        (tenant_id,),
    )
    return [_fee_view(r) for r in rows]


def get_fee_detail(conn: Any, *, settings: Settings, tenant_id: str,
                   fee_id: int) -> dict[str, Any]:
    """费用详情 + 四层溯源链（费用项→业务单据→作业事件→计费规则版本）+ 快照重算。"""
    fee = fetch_one(conn, "SELECT * FROM fee_details WHERE tenant_id=%s AND id=%s",
                    (tenant_id, fee_id))
    if fee is None:
        raise ErpError("NOT_FOUND", "费用明细不存在", detail={"fee_id": fee_id})
    prov = fee["provenance"] if isinstance(fee["provenance"], dict) else {}
    snap = fee["snapshot"] if isinstance(fee["snapshot"], dict) else {}
    rule = fetch_one(conn, "SELECT * FROM billing_rules WHERE tenant_id=%s AND id=%s",
                     (tenant_id, fee["rule_id"]))
    rule_cfg = rule["config"] if rule and isinstance(rule["config"], dict) else {}
    reversals = fetch_all(
        conn, "SELECT id, stable_id, amount, status, created_at FROM fee_details "
              "WHERE tenant_id=%s AND reversal_of=%s ORDER BY id", (tenant_id, fee_id))
    for r in reversals:
        r["amount"] = str(r["amount"])
        r["created_at"] = str(r["created_at"])

    jp = prov.get("journal") if isinstance(prov.get("journal"), dict) else {}
    txn_id = None
    if jp.get("stable_id"):
        posted = fetch_one(conn, "SELECT txn_id FROM journal_entries WHERE tenant_id=%s "
                                 "AND stable_id=%s LIMIT 1", (tenant_id, jp["stable_id"]))
        txn_id = posted["txn_id"] if posted else None

    event = prov.get("event") if isinstance(prov.get("event"), dict) else {}
    chain = {
        "fee_item": fee["fee_item"],
        "document": prov.get("document_refs"),
        "job_event": {"stable_id": event.get("stable_id"), "kind": event.get("kind"),
                      "occurred_at": event.get("occurred_at"),
                      "measures": event.get("measures")},
        "rule": {"rule_id": fee["rule_id"], "version": fee["rule_version"],
                 "rule_key": rule_cfg.get("rule_key"),
                 "rate_source": rule_cfg.get("rate_source"), "mode": rule_cfg.get("mode"),
                 "effective_from": rule_cfg.get("effective_from"),
                 "effective_to": rule_cfg.get("effective_to"),
                 "status": rule["status"] if rule else None},
    }
    # 快照重算：从锁定的 rule_config + measures_used 确定性重放，应与原金额一致。
    replay: dict[str, Any]
    try:
        replay_amount = compute_fee(snap.get("rule_config") or {}, snap.get("measures_used") or {})
        replay = {"amount": str(replay_amount),
                  "matches": replay_amount == Decimal(str(fee["amount"]))}
        # 本位币口径可复算：原币 × 锁定汇率 = base_amount（四要素自洽性）。
        if fee.get("base_amount") is None or fee.get("fx_rate") is None:
            replay["base_matches"] = fee.get("base_amount") is not None and \
                Decimal(str(fee["base_amount"])) == replay_amount
        else:
            replay_base = (replay_amount * Decimal(str(fee["fx_rate"]))).quantize(Q6)
            replay["base_amount"] = str(replay_base)
            replay["base_matches"] = replay_base == Decimal(str(fee["base_amount"]))
    except ErpError as exc:
        replay = {"amount": None, "matches": False, "error": exc.message}

    return {
        "fee": _fee_view(fee),
        "chain": chain,
        "fx": {
            "currency": fee["currency"],
            "original_amount": None if fee.get("original_amount") is None
                               else str(fee["original_amount"]),
            "base_amount": None if fee.get("base_amount") is None
                           else str(fee["base_amount"]),
            "rate": None if fee.get("fx_rate") is None else str(fee["fx_rate"]),
            "as_of": None if fee.get("fx_as_of") is None else str(fee["fx_as_of"]),
            "source": fee.get("fx_source"),
            "sourced": fee["currency"] == settings.base_currency
                       or bool(fee.get("fx_source")),
        },
        "journal": {"proposal_id": jp.get("proposal_id"),
                    "proposal_stable_id": jp.get("stable_id"), "txn_id": txn_id},
        "is_reversed": bool(reversals),
        "reversals": reversals,
        "snapshot_replay": replay,
        "snapshot": snap,
    }


# ---------------------------------------------------------------------------
# 试算（新旧规则费用差异；零写入）
# ---------------------------------------------------------------------------

def simulate_billing(conn: Any, *, settings: Settings, tenant_id: str,
                     config: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """按指定历史事件集对比新旧规则费用差异（试算零写入；费率出处纪律同正式规则）。"""
    cfg = validate_config(config, settings=settings)
    if not isinstance(events, list):
        raise ErpError("INVALID_ARGUMENT", "events 须为数组")
    actives = fetch_all(conn, "SELECT * FROM billing_rules WHERE tenant_id=%s "
                              "AND status='active'", (tenant_id,))
    old_candidates = [r for r in actives
                      if (r["config"] if isinstance(r["config"], dict) else {}).get("fee_item")
                      == cfg["fee_item"]]

    old_total = Decimal(0)
    new_total = Decimal(0)
    per_event: list[dict[str, Any]] = []
    skipped = 0
    for ev in events:
        _stable, kind, occurred, measures, _refs = _validate_event(ev)
        if kind != cfg["event_kind"]:
            skipped += 1
            continue
        new_amount, new_bd = compute_fee_with_breakdown(cfg, measures)
        old_amount = Decimal(0)
        old_rule_key = None
        matched_old = _match_event_rules(old_candidates, kind=kind, occurred=occurred)
        if matched_old:
            picked, _sup = _pick_top_by_item(matched_old)
            r0 = picked[0]
            c0 = r0["config"] if isinstance(r0["config"], dict) else json.loads(r0["config"])
            old_amount = compute_fee(c0, measures)
            old_rule_key = f"{r0['rule_key']}@v{r0['version']}"
        old_total += old_amount
        new_total += new_amount
        per_event.append({
            "stable_id": _stable, "occurred_at": str(occurred),
            "old_amount": str(old_amount.quantize(Q6)),
            "new_amount": str(new_amount.quantize(Q6)),
            "delta": str((new_amount - old_amount).quantize(Q6)),
            "old_rule": old_rule_key,
            "new_breakdown": {"mode": new_bd.get("mode"),
                              "applied_limits": new_bd.get("applied_limits")},
        })
    return {"diff_summary": {
        "fee_item": cfg["fee_item"],
        "mode": cfg["mode"],
        "rate_source": cfg["rate_source"],
        "event_count": len(events),
        "compared": len(per_event),
        "skipped_kind_mismatch": skipped,
        "old_total": str(old_total.quantize(Q6)),
        "new_total": str(new_total.quantize(Q6)),
        "delta": str((new_total - old_total).quantize(Q6)),
        "per_event": per_event,
    }}
