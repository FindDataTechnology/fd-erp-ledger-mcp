"""封闭错误码目录（mcp-error-semantics）。

规则：工具失败返回机器可读 code，SHALL NOT 仅自然语言；code 来自封闭集合，
新增码必须在 CATALOG 登记；至少四类语义可观测地区分：
  not_wired       未接线/未实现，重试无意义
  not_found       未找到，正常空
  upstream        上游不可用，可重试
  invalid_argument 入参非法，需修参
其余为账本域专属码，同样必须登记。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCode:
    code: str
    semantic_class: str
    retryable: bool
    default_hint: str


CATALOG: dict[str, ErrorCode] = {
    # 四类基础语义（可观测差异由 retryable + hint 承载）
    "NOT_WIRED": ErrorCode(
        "NOT_WIRED", "not_wired", False,
        "该能力未接线/未实现，重试无意义；请申请开通对应能力。",
    ),
    "NOT_FOUND": ErrorCode(
        "NOT_FOUND", "not_found", False,
        "对象不存在，属正常空结果；请核对标识后重试。",
    ),
    "UPSTREAM_UNAVAILABLE": ErrorCode(
        "UPSTREAM_UNAVAILABLE", "upstream", True,
        "上游（数据库/外部系统）暂时不可用，可稍后重试。",
    ),
    "INVALID_ARGUMENT": ErrorCode(
        "INVALID_ARGUMENT", "invalid_argument", False,
        "入参非法，请按提示修正参数后重试。",
    ),
    # 账本域专属
    "SCHEMA_OUTDATED": ErrorCode(
        "SCHEMA_OUTDATED", "not_wired", False,
        "数据库 schema 版本落后于代码要求；请先运行 `fd-erp-ledger-mcp migrate`。",
    ),
    "TENANT_DENIED": ErrorCode(
        "TENANT_DENIED", "invalid_argument", False,
        "跨租户访问被拒绝并已留审计痕；请只查询本租户数据。",
    ),
    "UNBALANCED": ErrorCode(
        "UNBALANCED", "invalid_argument", False,
        "借贷不平，数据库约束拒绝落账；请修正分录两侧金额。",
    ),
    "APPEND_ONLY": ErrorCode(
        "APPEND_ONLY", "invalid_argument", False,
        "已落账分录只进不改；更正须走冲正（红字/反向分录）车道。",
    ),
    "FX_UNSOURCED": ErrorCode(
        "FX_UNSOURCED", "invalid_argument", False,
        "跨币种分录缺少可溯汇率（rate/as_of/source）或 fx_facts 无对应事实；拒绝使用无出处汇率。",
    ),
    "PROPOSAL_STATE": ErrorCode(
        "PROPOSAL_STATE", "invalid_argument", False,
        "proposal 状态不允许该操作（仅 pending 可确认/拒绝）。",
    ),
    "RECON_TASK_NOT_FOUND": ErrorCode(
        "RECON_TASK_NOT_FOUND", "not_found", False,
        "对账任务不存在；请核对 task_id。",
    ),
    "INTERNAL": ErrorCode(
        "INTERNAL", "upstream", True,
        "服务内部错误，可重试；若持续请联系运维。",
    ),
}

# 至少四类可区分语义 —— selfcheck / 单测会断言该集合。
REQUIRED_CLASSES = {"not_wired", "not_found", "upstream", "invalid_argument"}


def lookup(code: str) -> ErrorCode:
    """未登记的码直接抛错：封闭集合，不允许临时造码。"""
    if code not in CATALOG:
        raise KeyError(f"error code {code!r} not registered in CATALOG")
    return CATALOG[code]


def assert_closed_catalog() -> list[str]:
    classes = {e.semantic_class for e in CATALOG.values()}
    missing = REQUIRED_CLASSES - classes
    if missing:
        raise AssertionError(f"error catalog missing semantic classes: {sorted(missing)}")
    return sorted(CATALOG)
