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
}


def annotations_for(tool_name: str) -> dict[str, Any]:
    if tool_name not in _ANNOTATIONS:
        raise KeyError(f"tool {tool_name!r} has no annotation")
    return dict(_ANNOTATIONS[tool_name])


def all_names() -> list[str]:
    return list(_ANNOTATIONS)
