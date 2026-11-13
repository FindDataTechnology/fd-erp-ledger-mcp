"""工具面行为属性（mcp-surface-contract / FastMCP tool annotations）。

与 tool-surface.json 的 tool_annotations 一致；selfcheck_surface.py 双向校对。
"""
from __future__ import annotations

from typing import Any

_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "submit_proposal":    {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "list_proposals":     {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "confirm_proposal":   {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": True, "openWorldHint": False},
    "reject_proposal":    {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "query_balance":      {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "query_entries":      {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "run_reconciliation": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "get_recon_report":   {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # 计费（add-supply-chain-vertical 任务 1.x）
    "submit_billing_rule": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "approve_billing_rule": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "list_billing_rules":  {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "bill_event":          {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "list_fees":           {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "get_fee_detail":      {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "reverse_fee":         {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "simulate_billing":    {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # 日终快照计费（change: optimize-erp-solution 5.3）——期间触发、幂等重跑
    "bill_period_snapshot": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # 信用（add-supply-chain-vertical 任务 4.2）
    "reserve_credit":      {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "release_credit":      {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "query_credit":        {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # 占用明细（change: close-orphan-credit-reservations）——孤儿报表的读原语
    "list_credit_reservations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # 事件层（change: establish-erp-event-kernel）
    "ingest_event":         {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "submit_event":         {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "decide_event":         {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": True,  "openWorldHint": False},
    "query_events":         {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    # 数量面
    "query_object_balance": {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "project_event":        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    # 过账层
    "submit_posting_rule":  {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "approve_posting_rule": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "list_posting_rules":   {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "post_event":           {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "list_posting_gaps":    {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    # 期间关账（change: add-period-close）——关账改写「哪期已报出」，属破坏性；
    # 重开同样是破坏性（会放开已关期间）。读面（状态/清单）只读。
    "close_period":         {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": True,  "openWorldHint": False},
    "reopen_period":        {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": False, "openWorldHint": False},
    "period_status":        {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "period_report":        {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "list_periods":         {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
}


def annotations_for(tool_name: str) -> dict[str, Any]:
    if tool_name not in _ANNOTATIONS:
        raise KeyError(f"tool {tool_name!r} has no annotation")
    return dict(_ANNOTATIONS[tool_name])


def all_names() -> list[str]:
    return list(_ANNOTATIONS)
