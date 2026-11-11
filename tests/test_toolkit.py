"""工具书闸五条判据的正负例（纯文件级，不需要 ERP_LEDGER_DATABASE_URL）。

判据与脚本的对应关系（每条都有正例与负例，见测试名后缀）：
  ① catalog ↔ 工具面双向相等 + 角色约束
  ② 卡内点名的工具 ⊆ 工具面
  ③ 每件工具至少被一张卡点名 + README 覆盖表
  ④ 每张卡四段齐全 + 纪律锚句在正文
  ⑤ 资金类科目不得出现在可写口径

负例都从一份「合法假仓」出发只改一处，确保红的不是别的判据连带出来的。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_toolkit as gate  # noqa: E402

pytestmark = pytest.mark.no_db


# --------------------------------------------------------------------------
# 合法假仓的基线
# --------------------------------------------------------------------------

SURFACE = {
    "schema": "mcp-tool-surface/v1",
    "tool_groups": [
        {"group": "alpha", "level": "commercial", "tools": ["query_alpha"]},
        {"group": "beta", "level": "commercial", "tools": ["submit_proposal", "confirm_proposal"]},
    ],
}

DISCIPLINES = [
    {"id": "d1", "anchor": "只出 proposal 或拟稿事件", "core": True},
    {"id": "d2", "anchor": "`__unknown__`", "core": True},
    {"id": "d3", "anchor": "余额不可直写", "core": True},
    {"id": "d4", "anchor": "缺规则挂待补", "core": True},
    {"id": "d5", "anchor": "资金类科目禁入", "core": False},
]

CATALOG = {
    "schema": "erp-kernel-toolkit-catalog/v1",
    "note": "说明书，不是新能力；工具面真源是 tool-surface.json。",
    "disciplines": DISCIPLINES,
    "groups": [
        {
            "id": "alpha",
            "tools": ["query_alpha"],
            "tool_roles": {"query_alpha": ["inspector", "analyst", "decider"]},
            "role_types": ["inspector", "analyst", "decider"],
            "discipline_ids": ["d1", "d2", "d3", "d4"],
            "forbidden": [{"what": "写数", "why": "只读组"}],
        },
        {
            "id": "beta",
            "tools": ["submit_proposal", "confirm_proposal"],
            "tool_roles": {
                "submit_proposal": ["analyst"],
                "confirm_proposal": ["decider"],
            },
            "role_types": ["analyst", "decider"],
            "discipline_ids": ["d1", "d2", "d3", "d4", "d5"],
            "forbidden": [{"what": "越过确认门", "why": "拟稿不是事实"}],
        },
    ],
}

CARD_ALPHA = """# alpha 组

> 工具：`query_alpha`

## 能做什么

只读巡检。

## 不许做什么

不许写任何状态。

## 硬纪律

1. agent 写路径只出 proposal 或拟稿事件。
2. 未知必须显式 `__unknown__`。
3. 余额不可直写。
4. 缺规则挂待补。

## 哪类岗位调哪几件

| 岗位 | 工具 |
|---|---|
| inspector | `query_alpha` |
"""

CARD_BETA = """# beta 组

> 工具：`submit_proposal` `confirm_proposal`

## 能做什么

拟稿与确认门。

## 不许做什么

不许把拟稿当事实。

## 硬纪律

1. agent 写路径只出 proposal 或拟稿事件。
2. 未知必须显式 `__unknown__`。
3. 余额不可直写。
4. 缺规则挂待补。
5. 资金类科目禁入（二清红线）。本组不执行代收代付，也不执行分账执行。

## 哪类岗位调哪几件

| 岗位 | 工具 |
|---|---|
| analyst | `submit_proposal` |
| decider | `confirm_proposal` |
"""

README = """# 工具书

本工具书是 agent 侧说明书，不是新能力。

| 工具 | 组 |
|---|---|
| `query_alpha` | alpha |
| `submit_proposal` | beta |
| `confirm_proposal` | beta |
"""


def make_repo(
    tmp_path: Path,
    *,
    surface: dict | None = None,
    catalog: dict | None = None,
    cards: dict[str, str] | None = None,
    readme: str | None = None,
) -> Path:
    """写一份合法假仓；每个参数只在测试要注入违规时才传。"""
    root = tmp_path / "repo"
    toolkit = root / "toolkit"
    (toolkit / "cards").mkdir(parents=True)
    (root / "tool-surface.json").write_text(
        json.dumps(surface if surface is not None else copy.deepcopy(SURFACE), ensure_ascii=False),
        encoding="utf-8",
    )
    (toolkit / "catalog.json").write_text(
        json.dumps(catalog if catalog is not None else copy.deepcopy(CATALOG), ensure_ascii=False),
        encoding="utf-8",
    )
    card_map = cards if cards is not None else {"alpha": CARD_ALPHA, "beta": CARD_BETA}
    for name, text in card_map.items():
        (toolkit / "cards" / f"{name}.md").write_text(text, encoding="utf-8")
    (toolkit / "README.md").write_text(readme if readme is not None else README, encoding="utf-8")
    return root


def problems_with(problems: list[str], needle: str) -> list[str]:
    return [p for p in problems if needle in p]


# --------------------------------------------------------------------------
# 正例：合法假仓全绿；⑤ 的否定语境不被误杀
# --------------------------------------------------------------------------

def test_valid_repo_passes(tmp_path: Path) -> None:
    code, problems, stats = gate.run_checks(make_repo(tmp_path))
    assert code == 0, problems
    assert stats["surface_tools"] == stats["catalog_tools"] == 3
    assert stats["readme_rows"] == 3


def test_negated_fund_sentence_is_accepted(tmp_path: Path) -> None:
    """⑤ 正例：同句含否定词（不/禁/不得/无/拒/零）时允许出现资金类词。"""
    cards = {"alpha": CARD_ALPHA, "beta": CARD_BETA + "\n本组禁止资金池、代收代付与分账执行。\n"}
    code, problems, _ = gate.run_checks(make_repo(tmp_path, cards=cards))
    assert code == 0, problems


# --------------------------------------------------------------------------
# ① 目录 ↔ 工具面（负例两种：缺 / 多），以及角色约束
# --------------------------------------------------------------------------

def test_catalog_missing_tool_is_blocked(tmp_path: Path) -> None:
    catalog = copy.deepcopy(CATALOG)
    catalog["groups"][1]["tools"].remove("submit_proposal")
    del catalog["groups"][1]["tool_roles"]["submit_proposal"]
    code, problems, _ = gate.run_checks(make_repo(tmp_path, catalog=catalog))
    assert code == 1
    assert problems_with(problems, "submit_proposal"), problems


def test_catalog_extra_tool_is_blocked(tmp_path: Path) -> None:
    catalog = copy.deepcopy(CATALOG)
    catalog["groups"][1]["tools"].append("ghost_tool")
    catalog["groups"][1]["tool_roles"]["ghost_tool"] = ["analyst"]
    code, problems, _ = gate.run_checks(make_repo(tmp_path, catalog=catalog))
    assert code == 1
    assert problems_with(problems, "ghost_tool"), problems


def test_group_level_misplacement_is_blocked(tmp_path: Path) -> None:
    """并集相同但组归属错：query_alpha 挪到 beta、submit_proposal 挪到 alpha。"""
    catalog = copy.deepcopy(CATALOG)
    catalog["groups"][0]["tools"] = ["submit_proposal"]
    catalog["groups"][1]["tools"] = ["query_alpha", "confirm_proposal"]
    code, problems, _ = gate.run_checks(make_repo(tmp_path, catalog=catalog))
    assert code == 1
    assert problems_with(problems, "组 alpha") or problems_with(problems, "组 beta"), problems


def test_write_tool_cannot_be_inspector(tmp_path: Path) -> None:
    catalog = copy.deepcopy(CATALOG)
    catalog["groups"][1]["tool_roles"]["submit_proposal"] = ["inspector", "analyst"]
    catalog["groups"][1]["role_types"] = ["inspector", "analyst", "decider"]
    code, problems, _ = gate.run_checks(make_repo(tmp_path, catalog=catalog))
    assert code == 1
    assert problems_with(problems, "写类工具 submit_proposal"), problems


def test_gate_tool_must_be_decider_only(tmp_path: Path) -> None:
    catalog = copy.deepcopy(CATALOG)
    catalog["groups"][1]["tool_roles"]["confirm_proposal"] = ["inspector", "decider"]
    catalog["groups"][1]["role_types"] = ["inspector", "analyst", "decider"]
    code, problems, _ = gate.run_checks(make_repo(tmp_path, catalog=catalog))
    assert code == 1
    assert problems_with(problems, "确认门工具 confirm_proposal"), problems


# --------------------------------------------------------------------------
# ② 卡内工具名 ⊆ 工具面
# --------------------------------------------------------------------------

def test_card_names_unknown_tool_is_blocked(tmp_path: Path) -> None:
    cards = {"alpha": CARD_ALPHA, "beta": CARD_BETA + "\n顺带调 `submit_ghost`。\n"}
    code, problems, _ = gate.run_checks(make_repo(tmp_path, cards=cards))
    assert code == 1
    assert problems_with(problems, "submit_ghost"), problems
    assert problems_with(problems, "[②]"), problems


def test_field_name_backticks_are_not_mistaken_for_tools(tmp_path: Path) -> None:
    """② 正例：`stable_id` / `__unknown__` 这类符号不是工具候选。"""
    cards = {"alpha": CARD_ALPHA + "\n字段 `stable_id` 与哨兵 `__unknown__` 不是工具。\n",
             "beta": CARD_BETA}
    code, problems, _ = gate.run_checks(make_repo(tmp_path, cards=cards))
    assert code == 0, problems


# --------------------------------------------------------------------------
# ③ 逐工具覆盖（卡 + README 表）
# --------------------------------------------------------------------------

def test_tool_not_named_by_any_card_is_blocked(tmp_path: Path) -> None:
    cards = {"alpha": CARD_ALPHA, "beta": CARD_BETA.replace("`submit_proposal`", "拟稿工具")}
    code, problems, _ = gate.run_checks(make_repo(tmp_path, cards=cards))
    assert code == 1
    assert problems_with(problems, "submit_proposal"), problems
    assert problems_with(problems, "[③]"), problems


def test_readme_table_missing_row_is_blocked(tmp_path: Path) -> None:
    readme = README.replace("| `submit_proposal` | beta |\n", "")
    code, problems, _ = gate.run_checks(make_repo(tmp_path, readme=readme))
    assert code == 1
    assert problems_with(problems, "README 覆盖表缺行"), problems
    assert problems_with(problems, "submit_proposal"), problems


# --------------------------------------------------------------------------
# ④ 卡四段齐全 + 纪律锚句
# --------------------------------------------------------------------------

def test_card_missing_discipline_heading_is_blocked(tmp_path: Path) -> None:
    cards = {"alpha": CARD_ALPHA.replace("## 硬纪律", "## 其他"), "beta": CARD_BETA}
    code, problems, _ = gate.run_checks(make_repo(tmp_path, cards=cards))
    assert code == 1
    assert problems_with(problems, "硬纪律"), problems


def test_card_missing_forbidden_section_is_blocked(tmp_path: Path) -> None:
    cards = {"alpha": CARD_ALPHA.replace("## 不许做什么", "## 其他"), "beta": CARD_BETA}
    code, problems, _ = gate.run_checks(make_repo(tmp_path, cards=cards))
    assert code == 1
    assert problems_with(problems, "不许做什么"), problems


def test_card_missing_discipline_anchor_is_blocked(tmp_path: Path) -> None:
    cards = {"alpha": CARD_ALPHA.replace("缺规则挂待补", "缺规则时再说"), "beta": CARD_BETA}
    code, problems, _ = gate.run_checks(make_repo(tmp_path, cards=cards))
    assert code == 1
    assert problems_with(problems, "缺规则挂待补"), problems


# --------------------------------------------------------------------------
# ⑤ 资金类科目不得出现在可写口径
# --------------------------------------------------------------------------

def test_fund_account_in_write_caliber_is_blocked(tmp_path: Path) -> None:
    cards = {"alpha": CARD_ALPHA, "beta": CARD_BETA + "\n本组可执行分账执行并写入资金池。\n"}
    code, problems, _ = gate.run_checks(make_repo(tmp_path, cards=cards))
    assert code == 1
    assert problems_with(problems, "[⑤]"), problems
    assert problems_with(problems, "资金池") or problems_with(problems, "分账执行"), problems


# --------------------------------------------------------------------------
# 退出码 2：出处缺失
# --------------------------------------------------------------------------

def test_missing_surface_is_exit_2(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "tool-surface.json").unlink()
    code, problems, _ = gate.run_checks(root)
    assert code == 2
    assert problems_with(problems, "出处缺失"), problems


def test_missing_catalog_is_exit_2(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "toolkit" / "catalog.json").unlink()
    code, problems, _ = gate.run_checks(root)
    assert code == 2
    assert problems_with(problems, "出处缺失"), problems


def test_missing_card_dir_is_exit_2(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "toolkit" / "cards").rename(root / "toolkit" / "cards_gone")
    code, problems, _ = gate.run_checks(root)
    assert code == 2
    assert problems_with(problems, "出处缺失"), problems


def test_cli_json_reports_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_repo(tmp_path)
    assert gate.main(["--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["exit_code"] == 0
