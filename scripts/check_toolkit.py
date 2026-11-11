#!/usr/bin/env python3
"""fd-erp-ledger-mcp · agent 侧工具书闸（change: establish-erp-kernel-toolkit-and-teams 1.4）。

判据是什么（五条，全部逐条输出问题列表）：
  ① 目录 == 工具面（双向）：toolkit/catalog.json 每组 tools 的并集必须与
     tool-surface.json 的 32 件逐名相等；同时逐组比对（组内多一件/少一件也点名）。
     目录是判据的锚：卡只有 Markdown 时，闸只能正则抓名字；有了 catalog，
     「哪些工具、属于哪组、什么角色可用」就成了可核对的声明。
     角色约束也放在这里：catalog 里把确认门工具标给 inspector/analyst、或把写类
     工具标给 inspector，属于目录自身不合法——它会让错误的授权看起来「有据可依」。
  ② 卡内点名的工具 ⊆ 工具面：卡里用反引号写的 `tool_name` 必须是真实符号。
     覆盖不到的反面是：工具改名/下线后，卡还在教 agent 调一个不存在的名字，
     闸若只查方向①不会发现（目录可以是对的，卡是错的）。
  ③ 覆盖：每件工具至少被一张卡点名（缺书即红），且 README 覆盖表逐行覆盖 32 件。
     说明书的价值在「逐工具」；只要有一件工具没有任何卡提到，agent 就不知道它
     存在，也不知道什么时候不该调它。README 表是给人看的同口径入口，允许它与
     工具面漂移等于允许两本说明书。
  ④ 卡结构：每张卡必须有「能做什么 / 不许做什么 / 硬纪律 / 哪类岗位调哪几件」
     四段（标题匹配，不限层级），且 catalog 声明的每条纪律锚句都能在正文找到。
     只写「能做什么」的卡会被 agent 读成「这些都能做」；纪律必须落在正文里，
     不能只存在于 catalog 的 id 列表。
  ⑤ 资金类科目不得出现在可写口径：卡内逐句扫描「资金池 / 代收代付 / 分账执行」，
     同一句里没有否定词（不 / 禁 / 不得 / 无 / 拒 / 零）即红。二清红线不因
     说明书写法而放宽；按句判定是因为「本组不执行分账执行」这种否定句必须合法。
     已知边界：同句出现否定词即豁免，无法识别「先否定后转折」的复杂句式——
     这条判据买的是「别把红线写成可执行」，不是语义理解。

为什么用「动词前缀」判定卡里的工具名：
  tool-surface.json 的 naming_convention 规定 32 件一律 `动词_名词`，动词封闭。
  卡里还会出现字段名（`stable_id`、`rule_key`）、哨兵（`__unknown__`）、错误码
  （`POSTING_RULE_MISSING`），把它们全当工具名会制造假阳性。因此只把
  「动词前缀 + 下划线」的符号、或「已在工具面里的符号」当工具候选。代价是
  改用全新动词命名的工具会漏检方向②——但方向③（该工具没人点名）与方向①
  （目录未跟工具面）仍会红，所以不会静默放行。

退出码：0 = 通过 / 1 = 阻断（有判据不通过）/ 2 = 出处缺失（工具面、catalog、
卡目录、README 缺一即 2——没有输入的闸全绿比没有闸更糟）。

纯标准库、离线运行：不 import fastmcp / psycopg / 本服务运行时模块。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SURFACE = REPO / "tool-surface.json"
DEFAULT_TOOLKIT = REPO / "toolkit"

# naming_convention: verb_noun（见 tool-surface.json 的 naming_convention 段）。
# 只用于「卡里这个反引号符号像不像工具名」，不是工具清单本身。
VERB_PREFIXES = frozenset({
    "submit", "list", "confirm", "reject", "query", "run", "get", "approve",
    "bill", "reverse", "simulate", "reserve", "release", "ingest", "decide",
    "project", "post",
})

ROLE_ORDER = ("inspector", "analyst", "decider")
ROLE_SET = frozenset(ROLE_ORDER)

#: 确认门工具：唯一能把拟稿变成事实的入口，只能标 decider。
GATE_TOOLS = frozenset({
    "confirm_proposal",
    "decide_event",
    "approve_billing_rule",
    "approve_posting_rule",
})

#: 写类工具：不得标 inspector（巡检只取数，不产生状态迁移）。
WRITE_TOOLS = frozenset({
    "submit_proposal",
    "ingest_event",
    "submit_event",
    "project_event",
    "post_event",
    "reserve_credit",
    "release_credit",
    "reverse_fee",
    "submit_billing_rule",
    "submit_posting_rule",
})

CARD_HEADINGS = ("能做什么", "不许做什么", "硬纪律", "哪类岗位调哪几件")
FUND_KEYWORDS = ("资金池", "代收代付", "分账执行")
NEGATION_CHARS = ("不", "禁", "不得", "无", "拒", "零")

HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
SYMBOL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
README_ROW_RE = re.compile(r"^\|\s*`([a-z][a-z0-9_]*)`\s*\|", re.MULTILINE)
SENTENCE_SPLIT_RE = re.compile(r"[。；;！!？?\n]+")


class SourceMissing(Exception):
    """出处缺失（退出码 2）：没有输入时不可判，也不可当成通过。"""


# --------------------------------------------------------------------------
# 读入
# --------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    if not path.exists():
        raise SourceMissing(f"缺文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise SourceMissing(f"不可解析：{path}（{err}）") from err
    if not isinstance(data, dict):
        raise SourceMissing(f"顶层不是 JSON 对象：{path}")
    return data


def load_surface(path: Path) -> dict:
    return _read_json(path)


def load_catalog(path: Path) -> dict:
    return _read_json(path)


def load_cards(cards_dir: Path) -> dict[str, str]:
    """返回 {group_id: 正文}；文件名（去 .md）就是组 id。"""
    if not cards_dir.is_dir():
        raise SourceMissing(f"缺卡目录：{cards_dir}")
    cards: dict[str, str] = {}
    for path in sorted(cards_dir.glob("*.md")):
        cards[path.stem] = path.read_text(encoding="utf-8")
    if not cards:
        raise SourceMissing(f"卡目录为空：{cards_dir}")
    return cards


def surface_model(surface: dict) -> tuple[set[str], dict[str, set[str]]]:
    """返回 (全部工具, {组名: 工具集})；只认 tool_groups（数组，每项有 tools）。"""
    groups: dict[str, set[str]] = {}
    all_tools: set[str] = set()
    for g in surface.get("tool_groups") or []:
        if not isinstance(g, dict):
            continue
        name = g.get("group")
        tools = g.get("tools") or []
        if not isinstance(name, str) or not isinstance(tools, list):
            continue
        groups[name] = set(t for t in tools if isinstance(t, str))
        all_tools.update(groups[name])
    return all_tools, groups


def catalog_model(catalog: dict) -> tuple[set[str], dict[str, set[str]], list[str]]:
    groups: dict[str, set[str]] = {}
    all_tools: set[str] = set()
    duplicates: list[str] = []
    for g in catalog.get("groups") or []:
        if not isinstance(g, dict):
            continue
        name = g.get("id")
        tools = g.get("tools") or []
        if not isinstance(name, str) or not isinstance(tools, list):
            continue
        clean = [t for t in tools if isinstance(t, str)]
        groups[name] = set(clean)
        for t in clean:
            if t in all_tools and t not in duplicates:
                duplicates.append(t)
        all_tools.update(clean)
    return all_tools, groups, duplicates


# --------------------------------------------------------------------------
# 判据
# --------------------------------------------------------------------------

def named_tools(text: str, surface_tools: set[str]) -> dict[str, int]:
    """卡内反引号里的工具候选 → 首次出现偏移。

    工具候选 = 已在工具面里的符号，或（动词前缀 + 下划线）的符号。
    字段名/哨兵/错误码因此不会进入候选，避免方向②的假阳性。
    """
    found: dict[str, int] = {}
    for m in BACKTICK_RE.finditer(text):
        token = m.group(1).strip()
        if not SYMBOL_RE.fullmatch(token):
            continue
        is_tool = token in surface_tools or token.split("_", 1)[0] in VERB_PREFIXES
        if is_tool:
            found.setdefault(token, m.start())
    return found


def check_catalog(
    surface_tools: set[str],
    surface_groups: dict[str, set[str]],
    catalog: dict,
) -> list[str]:
    """① 目录 == 工具面（双向 + 逐组 + 角色约束）。"""
    problems: list[str] = []
    if catalog.get("schema") != "erp-kernel-toolkit-catalog/v1":
        problems.append(f"[①] catalog.schema 非法：{catalog.get('schema')!r}")
    if not str(catalog.get("note") or "").strip():
        problems.append("[①] catalog.note 为空：工具书须自证「说明书不是新能力」")

    raw_groups = catalog.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        return problems + ["[①] catalog.groups 缺失或为空"]

    cat_tools, cat_groups, duplicates = catalog_model(catalog)
    known_disciplines = {
        d.get("id") for d in (catalog.get("disciplines") or [])
        if isinstance(d, dict) and isinstance(d.get("id"), str)
    }
    core_disciplines = {
        d.get("id") for d in (catalog.get("disciplines") or [])
        if isinstance(d, dict) and d.get("core") is True
    }

    if duplicates:
        problems.append(f"[①] 工具在多个组重复登记：{', '.join(sorted(duplicates))}")

    missing = sorted(surface_tools - cat_tools)
    extra = sorted(cat_tools - surface_tools)
    if missing:
        problems.append(
            f"[①] 工具面有、catalog 未覆盖（补卡，不要删工具）：{', '.join(missing)}"
        )
    if extra:
        problems.append(
            f"[①] catalog 登记了工具面不存在的工具（改名/下线未同步）：{', '.join(extra)}"
        )

    for gid in sorted(set(surface_groups) - set(cat_groups)):
        problems.append(f"[①] 工具面组 {gid!r} 在 catalog 缺登记")
    for gid in sorted(set(cat_groups) - set(surface_groups)):
        problems.append(f"[①] catalog 组 {gid!r} 在工具面不存在")

    for gid in sorted(set(surface_groups) & set(cat_groups)):
        s_tools, c_tools = surface_groups[gid], cat_groups[gid]
        only_surface = sorted(s_tools - c_tools)
        only_catalog = sorted(c_tools - s_tools)
        if only_surface:
            problems.append(f"[①] 组 {gid}: 工具面有、catalog 缺：{', '.join(only_surface)}")
        if only_catalog:
            problems.append(f"[①] 组 {gid}: catalog 多登记：{', '.join(only_catalog)}")

    # ---- 角色约束（目录自身的合法性）----
    for g in raw_groups:
        if not isinstance(g, dict):
            problems.append("[①] catalog.groups 含非对象项")
            continue
        gid = g.get("id")
        tools = [t for t in (g.get("tools") or []) if isinstance(t, str)]
        tool_roles = g.get("tool_roles") or {}
        role_types = g.get("role_types") or []
        if not isinstance(tool_roles, dict):
            problems.append(f"[①] 组 {gid}: tool_roles 必须是对象（工具 → 角色列表）")
            tool_roles = {}
        if not isinstance(role_types, list):
            problems.append(f"[①] 组 {gid}: role_types 必须是数组")
            role_types = []

        union: set[str] = set()
        for tool in tools:
            roles = tool_roles.get(tool)
            if not isinstance(roles, list) or not roles:
                problems.append(f"[①] 组 {gid}: 工具 {tool} 未标 role_types（每条工具都要标）")
                continue
            unknown = sorted(set(roles) - ROLE_SET)
            if unknown:
                problems.append(f"[①] 组 {gid}: 工具 {tool} 角色非法：{unknown}")
            union.update(r for r in roles if r in ROLE_SET)
            if tool in GATE_TOOLS and set(roles) != {"decider"}:
                problems.append(
                    f"[①] 组 {gid}: 确认门工具 {tool} 只能标 decider，实为 {roles}"
                )
            if tool in WRITE_TOOLS and "inspector" in roles:
                problems.append(
                    f"[①] 组 {gid}: 写类工具 {tool} 不得标 inspector，实为 {roles}"
                )
        stray = sorted(set(tool_roles) - set(tools))
        if stray:
            problems.append(f"[①] 组 {gid}: tool_roles 含组外工具：{', '.join(stray)}")

        expected_role_types = [r for r in ROLE_ORDER if r in union]
        if sorted(role_types) != sorted(expected_role_types):
            problems.append(
                f"[①] 组 {gid}: role_types {role_types} 与 tool_roles 并集 "
                f"{expected_role_types} 不一致"
            )

        discipline_ids = g.get("discipline_ids") or []
        if not isinstance(discipline_ids, list) or not discipline_ids:
            problems.append(f"[①] 组 {gid}: discipline_ids 为空（每组须挂纪律条款）")
            continue
        for did in discipline_ids:
            if did not in known_disciplines:
                problems.append(f"[①] 组 {gid}: discipline_id {did!r} 未在 disciplines 登记")
        for did in sorted(core_disciplines - set(discipline_ids)):
            problems.append(f"[①] 组 {gid}: 缺核心纪律 {did}")

        forbidden = g.get("forbidden")
        if not isinstance(forbidden, list) or not forbidden:
            problems.append(f"[①] 组 {gid}: forbidden 为空（须写明不许做什么）")
        else:
            for item in forbidden:
                if not isinstance(item, dict) or not str(item.get("what") or "").strip() \
                        or not str(item.get("why") or "").strip():
                    problems.append(f"[①] 组 {gid}: forbidden 项须含 what 与 why")

    return problems


def check_card_tools(
    surface_tools: set[str],
    cards: dict[str, str],
) -> list[str]:
    """② 卡内点名的工具 ⊆ 工具面。"""
    problems: list[str] = []
    for gid, text in sorted(cards.items()):
        for token in sorted(named_tools(text, surface_tools)):
            if token not in surface_tools:
                problems.append(
                    f"[②] 卡 {gid}.md 点名了工具面不存在的工具 `{token}`"
                    "（改名/下线/笔误；请改成存在的符号或补工具面）"
                )
    return problems


def check_coverage(
    surface_tools: set[str],
    cards: dict[str, str],
    readme: str,
) -> list[str]:
    """③ 每件工具至少被一卡点名；README 覆盖表逐行覆盖 32 件。"""
    problems: list[str] = []
    covered: set[str] = set()
    for text in cards.values():
        covered.update(named_tools(text, surface_tools))
    missing = sorted(surface_tools - covered)
    if missing:
        problems.append(f"[③] 以下工具没有任何卡点名（补卡，不要从工具面删）：{', '.join(missing)}")

    rows = README_ROW_RE.findall(readme)
    row_set = set(rows)
    dup = sorted({t for t in rows if rows.count(t) > 1})
    if dup:
        problems.append(f"[③] README 覆盖表重复行：{', '.join(dup)}")
    readme_missing = sorted(surface_tools - row_set)
    readme_extra = sorted(row_set - surface_tools)
    if readme_missing:
        problems.append(f"[③] README 覆盖表缺行：{', '.join(readme_missing)}")
    if readme_extra:
        problems.append(f"[③] README 覆盖表有工具面不存在的行：{', '.join(readme_extra)}")
    return problems


def _heading_positions(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(1).strip()) for m in HEADING_RE.finditer(text)]


def _section_body(text: str, headings: list[tuple[int, str]], title: str) -> str:
    for i, (start, heading) in enumerate(headings):
        if title in heading:
            end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
            return text[start:end]
    return ""


def check_card_structure(
    catalog: dict,
    cards: dict[str, str],
) -> list[str]:
    """④ 四段齐全 + 纪律锚句在正文里 + 卡与组一一对应。"""
    problems: list[str] = []
    cat_groups: dict[str, dict] = {}
    for g in catalog.get("groups") or []:
        if isinstance(g, dict) and isinstance(g.get("id"), str):
            cat_groups[g["id"]] = g
    anchors = {
        d.get("id"): d.get("anchor")
        for d in (catalog.get("disciplines") or [])
        if isinstance(d, dict) and isinstance(d.get("id"), str)
    }

    for gid in sorted(set(cat_groups) - set(cards)):
        problems.append(f"[④] 组 {gid!r} 没有对应卡 toolkit/cards/{gid}.md")
    for gid in sorted(set(cards) - set(cat_groups)):
        problems.append(f"[④] 卡 {gid}.md 对应的组不在 catalog 里")

    for gid, text in sorted(cards.items()):
        headings = _heading_positions(text)
        for title in CARD_HEADINGS:
            section = _section_body(text, headings, title)
            if not section:
                problems.append(f"[④] 卡 {gid}.md 缺「{title}」段（标题需含该词）")
            elif title in ("不许做什么", "硬纪律"):
                body = section.split("\n", 1)[1].strip() if "\n" in section else ""
                if not body:
                    problems.append(f"[④] 卡 {gid}.md 的「{title}」段为空")

        g = cat_groups.get(gid) or {}
        for did in g.get("discipline_ids") or []:
            anchor = anchors.get(did)
            if not anchor:
                problems.append(f"[④] 卡 {gid}.md: 纪律 {did} 在 catalog 缺 anchor")
            elif anchor not in text:
                problems.append(
                    f"[④] 卡 {gid}.md 缺纪律 {did} 的锚句：{anchor!r}"
                )
    return problems


def check_fund_accounts(cards: dict[str, str]) -> list[str]:
    """⑤ 资金类科目不得出现在可写口径（按句判定，同句有否定词即豁免）。"""
    problems: list[str] = []
    for gid, text in sorted(cards.items()):
        cursor = 0
        for m in SENTENCE_SPLIT_RE.finditer(text):
            fragment = text[cursor:m.start()]
            start = cursor
            cursor = m.end()
            hit = [k for k in FUND_KEYWORDS if k in fragment]
            if not hit:
                continue
            if any(ch in fragment for ch in NEGATION_CHARS):
                continue
            line = text.count("\n", 0, start) + 1
            snippet = " ".join(fragment.split())[:80]
            problems.append(
                f"[⑤] 卡 {gid}.md:{line} 把资金类科目写成可执行口径"
                f"（{', '.join(hit)}）：{snippet!r}；二清红线要求同句明确否定"
            )
        # 末尾残片
        fragment = text[cursor:]
        hit = [k for k in FUND_KEYWORDS if k in fragment]
        if hit and not any(ch in fragment for ch in NEGATION_CHARS):
            line = text.count("\n", 0, cursor) + 1
            snippet = " ".join(fragment.split())[:80]
            problems.append(
                f"[⑤] 卡 {gid}.md:{line} 把资金类科目写成可执行口径"
                f"（{', '.join(hit)}）：{snippet!r}；二清红线要求同句明确否定"
            )
    return problems


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def run_checks(
    root: Path | str = REPO,
    *,
    surface_path: Path | str | None = None,
    toolkit_dir: Path | str | None = None,
) -> tuple[int, list[str], dict]:
    """返回 (退出码, 问题列表, 统计)。退出码 0/1/2 语义见模块 docstring。"""
    root = Path(root)
    surface_path = Path(surface_path) if surface_path else root / "tool-surface.json"
    toolkit_dir = Path(toolkit_dir) if toolkit_dir else root / "toolkit"
    catalog_path = toolkit_dir / "catalog.json"
    cards_dir = toolkit_dir / "cards"
    readme_path = toolkit_dir / "README.md"

    try:
        surface = load_surface(surface_path)
        catalog = load_catalog(catalog_path)
        cards = load_cards(cards_dir)
        if not readme_path.exists():
            raise SourceMissing(f"缺文件：{readme_path}")
        readme = readme_path.read_text(encoding="utf-8")
    except SourceMissing as err:
        return 2, [f"[出处缺失] {err}（出处缺失不可判为通过）"], {"exit_code": 2}

    surface_tools, surface_groups = surface_model(surface)
    if not surface_tools:
        return 2, [f"[出处缺失] {surface_path} 没有可解析的 tool_groups 工具"], {"exit_code": 2}

    problems: list[str] = []
    problems += check_catalog(surface_tools, surface_groups, catalog)
    problems += check_card_tools(surface_tools, cards)
    problems += check_coverage(surface_tools, cards, readme)
    problems += check_card_structure(catalog, cards)
    problems += check_fund_accounts(cards)

    stats = {
        "surface_tools": len(surface_tools),
        "surface_groups": len(surface_groups),
        "catalog_tools": len(catalog_model(catalog)[0]),
        "catalog_groups": len(catalog_model(catalog)[1]),
        "cards": len(cards),
        "readme_rows": len(set(README_ROW_RE.findall(readme))),
    }
    return (1 if problems else 0), problems, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="agent 侧工具书闸：catalog/卡/README 与工具面三方一致 + 硬纪律与二清红线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--root", default=str(REPO), help="仓根（默认本脚本上两级）")
    ap.add_argument("--surface", default="", help="tool-surface.json 路径（默认 <root>/tool-surface.json）")
    ap.add_argument("--toolkit", default="", help="toolkit 目录（默认 <root>/toolkit）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    code, problems, stats = run_checks(
        args.root,
        surface_path=args.surface or None,
        toolkit_dir=args.toolkit or None,
    )

    if args.json:
        print(json.dumps(
            {"ok": code == 0, "exit_code": code, "problems": problems, "stats": stats},
            ensure_ascii=False, indent=2,
        ))
        return code

    print("工具书闸 · agent 侧说明书 ↔ 工具面")
    print(f"  工具面 : {stats.get('surface_tools', '?')} 工具 / {stats.get('surface_groups', '?')} 组")
    print(f"  catalog: {stats.get('catalog_tools', '?')} 工具 / {stats.get('catalog_groups', '?')} 组")
    print(f"  卡     : {stats.get('cards', '?')} 张 / README 行 {stats.get('readme_rows', '?')}")
    print("-" * 62)
    if problems:
        print(f"✗ 阻断 — {len(problems)} 项：")
        for p in problems:
            print(f"  - {p}")
    else:
        print("✓ 通过 — ①目录双向一致 ②卡内符号可解析 ③逐工具覆盖 ④四段与纪律锚句 ⑤资金类科目无写入口径")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
