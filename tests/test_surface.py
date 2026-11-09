"""工具面（2.1/2.5 + add-supply-chain-vertical 1.x/4.2 + close-orphan-credit-reservations）：
20 工具全暴露、行为属性齐全、错误语义对齐。"""
from __future__ import annotations

import json
from pathlib import Path

from fd_erp_ledger_mcp.annotations import all_names, annotations_for

EXPECTED_TOOLS = {
    "submit_proposal", "list_proposals", "confirm_proposal", "reject_proposal",
    "query_balance", "query_entries", "run_reconciliation", "get_recon_report",
    # 计费（add-supply-chain-vertical 任务 1.x）
    "submit_billing_rule", "approve_billing_rule", "list_billing_rules",
    "bill_event", "list_fees", "get_fee_detail", "reverse_fee", "simulate_billing",
    # 信用（add-supply-chain-vertical 任务 4.2 + close-orphan-credit-reservations）
    "reserve_credit", "release_credit", "query_credit", "list_credit_reservations",
}
EXPECTED_TOOL_COUNT = 20


def test_runtime_annotations_cover_exact_set():
    assert set(all_names()) == EXPECTED_TOOLS
    assert len(EXPECTED_TOOLS) == EXPECTED_TOOL_COUNT


def test_manifest_matches_runtime_truth():
    """tool-surface.json ↔ 运行时注解 双向一致（mcp-surface-contract）。"""
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "tool-surface.json").read_text(encoding="utf-8"))
    declared = set(manifest["tool_annotations"])
    assert declared == EXPECTED_TOOLS
    commercial = set()
    for group in manifest["tool_groups"]:
        if group["level"] == "commercial":
            commercial.update(group["tools"])
    assert commercial == EXPECTED_TOOLS
    assert manifest["registry"]["expected_tool_count"] == EXPECTED_TOOL_COUNT
    for name, attrs in manifest["tool_annotations"].items():
        rt = annotations_for(name)
        for k, v in rt.items():
            assert attrs[k] == v, f"{name}.{k}: manifest={attrs[k]} runtime={v}"


def test_read_only_tools_flagged():
    for name in ("list_proposals", "query_balance", "query_entries", "get_recon_report",
                 "list_billing_rules", "list_fees", "get_fee_detail", "simulate_billing",
                 "query_credit"):
        assert annotations_for(name)["readOnlyHint"] is True
    for name in ("submit_proposal", "confirm_proposal", "reject_proposal",
                 "run_reconciliation", "submit_billing_rule", "approve_billing_rule",
                 "bill_event", "reverse_fee", "reserve_credit", "release_credit"):
        assert annotations_for(name)["readOnlyHint"] is False


def test_no_direct_posting_tool():
    """写路径只出 proposal：不存在 post_entry / write_journal 之类的直写工具。"""
    forbidden = {"post_entry", "write_journal", "post_journal", "insert_entry",
                 "post_fee", "write_journal_direct"}
    assert EXPECTED_TOOLS & forbidden == set()


def test_error_codes_declared_in_manifest():
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "tool-surface.json").read_text(encoding="utf-8"))
    for name, group in manifest["tool_annotations"].items():
        codes = group.get("error_codes") or []
        assert codes, f"{name} 未声明错误码集合"
        for code in codes:
            from fd_erp_ledger_mcp.error_codes import CATALOG
            assert code in CATALOG, f"{name} 声明了未登记错误码 {code}"
