#!/usr/bin/env python3
"""fd-erp-ledger-mcp · tool-surface manifest 双向 selfcheck（mcp-surface-contract）。

纯标准库、离线运行：不 import fastmcp / psycopg，也不 import 本服务任何运行时模块。
用 `python3 fd-erp-ledger-mcp/scripts/selfcheck_surface.py` 即可跑（无需安装依赖）。

双向断言（对应 spec「selfcheck 双向断言」）：
  方向 1 · 一致（manifest ↔ 运行时注册真值）：
      manifest 的 tool_annotations / tool_groups 工具集，必须与 canonical 32 工具集
      （hardcode：8 基础账本 + 计费 9 + 信用 4，add-supply-chain-vertical 任务 1.x/4.2
      + optimize-erp-solution 5.3 的 bill_period_snapshot）
      逐名相等。
      由于离线不加载运行时，canonical 集合即运行时注册真值的离线镜像；
      新增/删除工具而 manifest 未同步 → 失败并点名差异。
  方向 2 · 闭合（对外暴露集 ⊆ commercial 集）：
      消费侧/网关欲暴露的工具集必须落在 manifest 的 commercial 集内。
      默认暴露集 = canonical 32；可用 --expose 传入逗号分隔工具名模拟某个 pack
      的声明，越界（点名 internal/不存在工具）即失败。

另有静态完整性断言（闭环，不依赖运行时）：
  - registry.expected_tool_count == 32
  - 每个 commercial 工具声明齐全的行为属性（只读/破坏性/幂等/openWorld 四 hint）
  - 封闭错误码集合非空、语义类 ≥4 类可区分、且与运行时 CATALOG 镜像一致；
    每个工具自报的 error_codes ⊆ 该封闭集合（不允许临时造码）
  - output_structure 的 declared ∪ undeclared == 全 32 工具且互斥（无静默空缺），
    每个 declared 工具都有 tool_fields
  - agent 侧工具书（toolkit/catalog.json）与工具面同口径：组数、组名、每组工具集
    与 canonical 32 逐组相等（说明书可以改措辞，但不能改口径）。卡/README 的
    逐段判据在 scripts/check_toolkit.py；这里只钉住「目录 == 工具面」这一层，
    让工具面自检发现「工具面加了工具但工具书没跟上」。

退出码：0 = PASS / 1 = FAIL（逐条列出差异）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "tool-surface.json"
TOOLKIT_CATALOG = REPO / "toolkit" / "catalog.json"
SERVER_NAME = "fd-erp-ledger-mcp"

# ---- canonical 真值（offline 镜像；改动请先改 /tmp/erp-kernel-contract.md）----

# 32 EXACT commercial 工具名（账本 8 + 计费 9 + 信用 4 + 事件 4 + 数量面 2 + 过账 5）
CANONICAL_TOOLS = frozenset({
    "submit_proposal",
    "list_proposals",
    "confirm_proposal",
    "reject_proposal",
    "query_balance",
    "query_entries",
    "run_reconciliation",
    "get_recon_report",
    # 计费（add-supply-chain-vertical 任务 1.x）
    "submit_billing_rule",
    "approve_billing_rule",
    "list_billing_rules",
    "bill_event",
    "list_fees",
    "get_fee_detail",
    "reverse_fee",
    "simulate_billing",
    # 日终快照计费（optimize-erp-solution 5.3）——期间触发、幂等重跑
    "bill_period_snapshot",
    # 信用（add-supply-chain-vertical 任务 4.2）
    "reserve_credit",
    "release_credit",
    "query_credit",
    # 占用明细（close-orphan-credit-reservations）——只读，孤儿报表的读原语
    "list_credit_reservations",
    # 事件层（establish-erp-event-kernel）
    "ingest_event",
    "submit_event",
    "decide_event",
    "query_events",
    # 数量面（对象投影）
    "query_object_balance",
    "project_event",
    # 过账层
    "submit_posting_rule",
    "approve_posting_rule",
    "list_posting_rules",
    "post_event",
    "list_posting_gaps",
    # 期间关账（change: add-period-close）
    "close_period",
    "reopen_period",
    "period_status",
    "period_report",
    "list_periods",
})
EXPECTED_TOOL_COUNT = 37

# 行为属性（mcp-surface-contract「只读/破坏性/幂等」+ openWorld 协议层第四项）
REQUIRED_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# 封闭错误码集合：镜像 fd_erp_ledger_mcp/error_codes.py 的 CATALOG（运行时真值）。
# selfcheck 刻意不 import 该模块（保持离线/stdlib-only），以常量镜像做方向 1 的
# 一致性核对；manifest 若与此处漂移即判失败。
CANONICAL_ERROR_CODES = frozenset({
    "NOT_WIRED",
    "NOT_FOUND",
    "UPSTREAM_UNAVAILABLE",
    "INVALID_ARGUMENT",
    "SCHEMA_OUTDATED",
    "TENANT_DENIED",
    "UNBALANCED",
    "APPEND_ONLY",
    "FX_UNSOURCED",
    "PERIOD_CLOSED",
    "PROPOSAL_STATE",
    "RECON_TASK_NOT_FOUND",
    # 计费域（add-supply-chain-vertical 任务 1.x）
    "RATE_UNSOURCED",
    "RULE_CONFLICT",
    "RULE_STATE",
    # 收费项覆盖缺失（optimize-erp-solution 5.5）
    "FEE_COVERAGE_MISSING",
    # 信用域（add-supply-chain-vertical 任务 4.2）
    "CREDIT_EXCEEDED",
    # 事件层（establish-erp-event-kernel）
    "EVENT_IMMUTABLE",
    "EVENT_UNKNOWN_TYPE",
    "EVENT_STATE",
    "OBJECT_CONFLICT",
    "POSTING_RULE_MISSING",
    # 营销接入（fuse-marketing-onto-event-kernel 1.3-1.5）：前缀不自洽、无载体事实
    "EVENT_PRODUCER_MISMATCH",
    "EVENT_FABRICATED_FACT",
    "INTERNAL",
})
# 至少四类可区分语义（error_codes.py 的 REQUIRED_CLASSES）
CANONICAL_SEMANTIC_CLASSES = frozenset({
    "not_wired",
    "not_found",
    "upstream",
    "invalid_argument",
})
MIN_SEMANTIC_CLASSES = 4


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        print(f"[加载失败] tool-surface.json 不是合法 JSON：{err}", file=sys.stderr)
        return {}


def group_tools(m: dict) -> tuple[set[str], set[str], list[str]]:
    """返回 (all_tools, commercial_tools, illegal_level_problems)。"""
    all_tools: set[str] = set()
    commercial: set[str] = set()
    problems: list[str] = []
    for g in m.get("tool_groups", []):
        level = g.get("level")
        if level not in ("commercial", "internal"):
            problems.append(f"tool_groups.{g.get('group')} 的 level 非法：{level!r}")
            continue
        for name in g.get("tools", []):
            if name in all_tools:
                problems.append(f"工具 {name} 在多个 group 中重复登记")
            all_tools.add(name)
            if level == "commercial":
                commercial.add(name)
    return all_tools, commercial, problems


def check_toolkit_alignment(m: dict, surface_tools: set[str]) -> list[str]:
    """工具书 catalog ↔ 工具面：组数/组名/每组工具集逐组相等。

    工具面是能力真源，工具书是说明书；两者可以措辞不同，但「有哪些组、每组有
    哪些工具」必须同口径。新增工具而工具书未跟进 → 失败并提示补卡（不是删工具）。
    """
    problems: list[str] = []
    if not TOOLKIT_CATALOG.exists():
        return [f"[工具书缺失] {TOOLKIT_CATALOG} 不存在（工具面须配 agent 侧说明书）"]
    try:
        catalog = json.loads(TOOLKIT_CATALOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return [f"[工具书不可解析] {TOOLKIT_CATALOG}：{err}"]

    groups = catalog.get("groups")
    if not isinstance(groups, list) or not groups:
        return ["[工具书缺失] catalog.groups 为空（8 组工具书须逐组登记）"]

    surface_groups: dict[str, set[str]] = {}
    for g in m.get("tool_groups") or []:
        surface_groups[g.get("group")] = set(g.get("tools") or [])

    cat_groups: dict[str, set[str]] = {}
    cat_all: set[str] = set()
    for g in groups:
        gid = g.get("id")
        tools = {t for t in (g.get("tools") or []) if isinstance(t, str)}
        if not isinstance(gid, str):
            problems.append("[工具书口径] catalog 组缺 id")
            continue
        if gid in cat_groups:
            problems.append(f"[工具书口径] catalog 组重复：{gid}")
        cat_groups[gid] = tools
        cat_all.update(tools)

    for gid in sorted(set(surface_groups) - set(cat_groups)):
        problems.append(f"[工具书口径] 工具面组 {gid!r} 在 catalog 缺登记")
    for gid in sorted(set(cat_groups) - set(surface_groups)):
        problems.append(f"[工具书口径] catalog 组 {gid!r} 在工具面不存在")
    for gid in sorted(set(surface_groups) & set(cat_groups)):
        only_surface = sorted(surface_groups[gid] - cat_groups[gid])
        only_catalog = sorted(cat_groups[gid] - surface_groups[gid])
        if only_surface:
            problems.append(f"[工具书口径] 组 {gid}: 工具面有、catalog 缺：{', '.join(only_surface)}")
        if only_catalog:
            problems.append(f"[工具书口径] 组 {gid}: catalog 多登记：{', '.join(only_catalog)}")

    if cat_all != surface_tools:
        problems.append(
            "[工具书口径] catalog 工具并集与工具面不符："
            f"缺 {sorted(surface_tools - cat_all)} / 多 {sorted(cat_all - surface_tools)}"
        )
    return problems


def check() -> tuple[list[str], dict]:
    problems: list[str] = []
    m = load_manifest()
    if not m:
        return [f"manifest 缺失或不可解析：{MANIFEST}（契约：缺失即未上牌）"], {}

    # ---- 基本身份 ----
    if m.get("schema") != "mcp-tool-surface/v1":
        problems.append(f"schema 非法：{m.get('schema')!r}（应为 mcp-tool-surface/v1）")
    if m.get("server") != SERVER_NAME:
        problems.append(f"server 非法：{m.get('server')!r}（应为 {SERVER_NAME}）")
    if m.get("entry_path") != "/fd-erp-ledger-mcp":
        problems.append(f"entry_path 非法：{m.get('entry_path')!r}（应为 /fd-erp-ledger-mcp）")

    ann = m.get("tool_annotations")
    if not isinstance(ann, dict):
        problems.append("manifest 缺 tool_annotations（行为属性须随工具一并上牌）")
        ann = {}
    ann_tools = set(ann)

    g_tools, commercial, g_problems = group_tools(m)
    problems += g_problems

    # ---- 工具书 catalog ↔ 工具面（说明书不得改口径）----
    problems += check_toolkit_alignment(m, g_tools)

    # ---- 方向 1：一致（manifest ↔ canonical 运行时真值），两个方向都查 ----
    # (b) tool_annotations + tool_groups 的工具集 == canonical 8
    for label, declared in (("tool_annotations", ann_tools), ("tool_groups", g_tools)):
        missing = sorted(CANONICAL_TOOLS - declared)      # 运行时有、manifest 漏登记
        stale = sorted(declared - CANONICAL_TOOLS)        # manifest 多登记/陈旧
        if missing:
            problems.append(f"[注册表漂移/{label}] 运行时已注册但 manifest 未声明：{', '.join(missing)}")
        if stale:
            problems.append(f"[manifest 陈旧/{label}] manifest 声明但运行时未注册：{', '.join(stale)}")
    if ann_tools != g_tools:
        problems.append(
            "[manifest 自相矛盾] tool_annotations 与 tool_groups 工具集不一致："
            f"仅 annotations={sorted(ann_tools - g_tools)} / 仅 groups={sorted(g_tools - ann_tools)}"
        )

    # ---- (c) 工具数 ----
    expected = m.get("registry", {}).get("expected_tool_count")
    if expected != EXPECTED_TOOL_COUNT:
        problems.append(f"[工具数不符] registry.expected_tool_count={expected!r} ≠ {EXPECTED_TOOL_COUNT}")
    if len(CANONICAL_TOOLS) != EXPECTED_TOOL_COUNT:
        problems.append(f"[常量异常] CANONICAL_TOOLS 大小 {len(CANONICAL_TOOLS)} ≠ {EXPECTED_TOOL_COUNT}")

    # ---- (d) 每个 commercial 工具行为属性齐全 ----
    for name in sorted(commercial):
        d = ann.get(name)
        if not isinstance(d, dict):
            problems.append(f"{name}: commercial 工具缺 tool_annotations 行为属性")
            continue
        missing_hints = [k for k in REQUIRED_HINTS if not isinstance(d.get(k), bool)]
        if missing_hints:
            problems.append(f"{name}: 行为属性不全/非布尔，缺 {missing_hints}")
        # 只读工具不得声明为破坏性（属性与副作用矛盾的静态护栏）
        if d.get("readOnlyHint") is True and d.get("destructiveHint") is True:
            problems.append(f"{name}: 矛盾——只读工具却声明 destructiveHint=true")

    # ---- (e) 封闭错误码集合 ----
    ec = m.get("error_codes")
    if not isinstance(ec, dict):
        problems.append("manifest 缺 error_codes 封闭集合声明")
    else:
        if ec.get("closed_set") is not True:
            problems.append("error_codes.closed_set 应为 true（封闭集合）")
        codes = ec.get("codes")
        if not isinstance(codes, list) or not codes:
            problems.append("error_codes.codes 为空/非法（封闭集合须非空）")
        else:
            declared_codes = set(codes)
            if len(declared_codes) != len(codes):
                problems.append("error_codes.codes 含重复项")
            drift_add = sorted(declared_codes - CANONICAL_ERROR_CODES)
            drift_missing = sorted(CANONICAL_ERROR_CODES - declared_codes)
            if drift_add:
                problems.append(f"[错误码漂移] manifest 声明了运行时 CATALOG 未登记的码：{', '.join(drift_add)}")
            if drift_missing:
                problems.append(f"[错误码漂移] 运行时 CATALOG 有、manifest 漏声明：{', '.join(drift_missing)}")
        classes = ec.get("semantic_classes")
        if not isinstance(classes, list) or len(set(classes)) < MIN_SEMANTIC_CLASSES:
            problems.append(
                f"error_codes.semantic_classes 需 ≥{MIN_SEMANTIC_CLASSES} 类可区分，实为 {classes!r}"
            )
        elif set(classes) != CANONICAL_SEMANTIC_CLASSES:
            problems.append(
                "[错误码漂移] semantic_classes 与运行时 REQUIRED_CLASSES 不符："
                f"manifest={sorted(set(classes))} / canonical={sorted(CANONICAL_SEMANTIC_CLASSES)}"
            )
        # 每个工具自报的 error_codes 必须 ⊆ 封闭集合（不允许临时造码）
        for name in sorted(ann_tools):
            d = ann.get(name)
            if not isinstance(d, dict):
                continue
            tool_codes = d.get("error_codes")
            if not isinstance(tool_codes, list) or not tool_codes:
                problems.append(f"{name}: 未声明 error_codes（每工具须带错误码集合）")
                continue
            undeclared = sorted(set(tool_codes) - CANONICAL_ERROR_CODES)
            if undeclared:
                problems.append(f"{name}: 自报错码越出封闭集合：{', '.join(undeclared)}")

    # ---- output_structure：无静默空缺 ----
    osb = m.get("output_structure")
    if not isinstance(osb, dict):
        problems.append("manifest 缺 output_structure")
    else:
        declared = set(osb.get("declared") or [])
        undeclared = set(osb.get("undeclared") or [])
        both = sorted(declared & undeclared)
        silent = sorted(CANONICAL_TOOLS - declared - undeclared)
        stray = sorted((declared | undeclared) - CANONICAL_TOOLS)
        if both:
            problems.append(f"output_structure 同时 declared 与 undeclared：{both}")
        if silent:
            problems.append(f"output_structure 既未声明也未登记为未声明（静默空缺）：{silent}")
        if stray:
            problems.append(f"output_structure 登记了未上牌工具：{stray}")
        fields = osb.get("tool_fields") or {}
        for t in sorted(declared):
            if not fields.get(t):
                problems.append(f"output_structure.declared 工具 {t} 缺 tool_fields 字段结构")
        for t in ("list_proposals", "query_entries", "get_recon_report"):
            if t not in declared:
                problems.append(f"分页工具 {t} 未声明 output_structure（task 2.5 要求）")

    return problems, {"manifest": m, "commercial": commercial, "ann": ann}


def check_closure(commercial: set[str], expose: set[str]) -> list[str]:
    """方向 2：对外暴露集 ⊆ commercial 集。"""
    problems: list[str] = []
    over = sorted(expose - commercial)
    if over:
        problems.append(
            f"[消费侧越界] 暴露集含非 commercial/未上牌工具：{', '.join(over)}"
            "（manifest 未定级 commercial，不得进商业供给面）"
        )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--expose",
        default="",
        help="逗号分隔的工具名，模拟某 pack/消费侧声明的暴露集（默认=canonical 32）。用于验证闭合方向。",
    )
    args = ap.parse_args()

    problems, ctx = check()
    commercial = ctx.get("commercial", set())
    exposure = (
        {t.strip() for t in args.expose.split(",") if t.strip()}
        if args.expose.strip()
        else set(CANONICAL_TOOLS)
    )
    problems += check_closure(commercial, exposure)

    # ---- 打印摘要 ----
    print(f"server        : {SERVER_NAME}")
    print(f"manifest      : {MANIFEST}")
    print(f"canonical     : {len(CANONICAL_TOOLS)} 工具（账本 8 + 计费 9 + 信用 4 + 事件 4 + 数量面 2 + 过账 5）")
    if ctx.get("manifest"):
        m = ctx["manifest"]
        print(f"声明            : annotations={len(ctx['ann'])} / "
              f"groups={sum(len(g.get('tools', [])) for g in m.get('tool_groups', []))} / "
              f"commercial={len(commercial)}")
        ec = m.get("error_codes", {})
        print(f"错误码          : {len(ec.get('codes', []))} 个封闭码 / "
              f"{len(set(ec.get('semantic_classes', [])))} 类语义")
        try:
            tk = json.loads(TOOLKIT_CATALOG.read_text(encoding="utf-8")) if TOOLKIT_CATALOG.exists() else {}
        except (OSError, json.JSONDecodeError):
            tk = {}
        tk_groups = tk.get("groups") or []
        print(f"工具书          : {len(tk_groups)} 组 / "
              f"{sum(len(g.get('tools', [])) for g in tk_groups if isinstance(g, dict))} 工具")
    print(f"方向1 一致     : manifest ↔ canonical 运行时真值")
    print(f"方向2 闭合     : 暴露集({len(exposure)}) ⊆ commercial({len(commercial)})")
    print("-" * 60)

    if problems:
        print(f"✗ FAIL — {len(problems)} 项不通过：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("✓ PASS — 双向断言通过（manifest ↔ 运行时一致 / 暴露 ⊆ commercial）")
    print("  离线 stdlib-only：未 import fastmcp / psycopg / 运行时模块。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
