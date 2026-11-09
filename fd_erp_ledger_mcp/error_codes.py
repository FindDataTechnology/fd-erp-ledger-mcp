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
    # 计费域（add-supply-chain-vertical 任务 1.x）
    "RATE_UNSOURCED": ErrorCode(
        "RATE_UNSOURCED", "invalid_argument", False,
        "计价参数缺少出处（config.rate_source 为空即拒绝）；拒绝无出处费率，"
        "请附规则费率的来源引用（如价目表/合同编号）。",
    ),
    "RULE_CONFLICT": ErrorCode(
        "RULE_CONFLICT", "invalid_argument", False,
        "同收费维度规则冲突未处置（区间相交且无显式优先级，或生效期存在空洞）；"
        "上线被拦截，请先处置冲突（补优先级/补齐覆盖/退役旧规则）。",
    ),
    "RULE_STATE": ErrorCode(
        "RULE_STATE", "invalid_argument", False,
        "计费规则状态不允许该操作（仅 pending_approval 可审批上线）。",
    ),
    "FEE_COVERAGE_MISSING": ErrorCode(
        "FEE_COVERAGE_MISSING", "invalid_argument", False,
        "收费项覆盖缺失：登记在用的收费项没有 active 规则（或计费命中未登记/无规则的"
        "收费项）；detail.fee_items 点名缺失项，detail.stage 标明拦截点"
        "（rule_approval / event_billing / period_billing）。",
    ),
    # 信用域（add-supply-chain-vertical 任务 4.2）
    "CREDIT_EXCEEDED": ErrorCode(
        "CREDIT_EXCEEDED", "invalid_argument", False,
        "信用额度不足；detail 含额度明细（上限/应收余额/已占用/本次请求/可用）。",
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
