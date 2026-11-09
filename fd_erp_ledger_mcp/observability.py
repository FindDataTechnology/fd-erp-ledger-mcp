"""运行指标与结构化 JSON 日志（change: optimize-erp-solution 任务 6.1，内核侧）。

设计（design.md D11）：`prometheus_client` 暴露 `/metrics`，日志用 JSON 行。
覆盖四类运行信号：
  * proposal 提交 / 确认 / 拒绝计数；
  * 计费笔数与失败原因分布（原因 = 封闭错误码，可归因、不合并为单一错误计数）；
  * 对账任务状态与差异数（按四类差异分别计）；
  * 工具请求时延（histogram）与错误码分布。

红线（BYOK 零落库 + 6.1 禁止泄漏）：指标与日志 **MUST NOT** 含凭据、连接串或
租户明文。租户只以 12 位 sha256 前缀出现（`tenant_hash`）；疑似 DSN / 凭证字段在
`redact()` 里一律打码——这是落盘前的最后一道闸，不是靠调用方自觉。

`/metrics` 的暴露面纪律：只绑集群内地址、不挂公网 Ingress（清单侧约束，见 README）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

from prometheus_client import (CONTENT_TYPE_LATEST, CollectorRegistry, Counter,
                               Histogram, generate_latest)

#: 独立 registry：不污染 prometheus_client 默认 PROCESS/PLATFORM 收集器，
#: 也让 `/metrics` 输出可预测（测试能断言"没有预期外的敏感标签"）。
REGISTRY = CollectorRegistry(auto_describe=True)

# ---- proposal 车道 ----
PROPOSALS_SUBMITTED = Counter(
    "erp_proposals_submitted_total",
    "提交的 proposal 数（按 kind 区分 journal/reversal）",
    ["kind"], registry=REGISTRY,
)
PROPOSALS_CONFIRMED = Counter(
    "erp_proposals_confirmed_total",
    "经确认门落账的 proposal 数",
    [], registry=REGISTRY,
)
PROPOSALS_REJECTED = Counter(
    "erp_proposals_rejected_total",
    "被拒绝的 proposal 数（不落账，留审计痕）",
    [], registry=REGISTRY,
)

# ---- 计费 ----
FEES_BILLED = Counter(
    "erp_fees_billed_total",
    "生成的费用明细笔数（append-only 事实行）",
    ["path", "currency"], registry=REGISTRY,
)
BILLING_FAILURES = Counter(
    "erp_billing_failures_total",
    "计费失败次数（reason = 封闭错误码：缺规则/缺汇率/无出处/入参…，可归因）",
    ["reason"], registry=REGISTRY,
)

# ---- 对账 ----
RECON_TASKS = Counter(
    "erp_recon_tasks_total",
    "对账任务终态计数（done/failed/interrupted）",
    ["status"], registry=REGISTRY,
)
RECON_DIFFS = Counter(
    "erp_recon_diffs_total",
    "对账差异条目数（按四类差异分别计）",
    ["diff_type"], registry=REGISTRY,
)
RECON_RESTART_DISPOSITION = Counter(
    "erp_recon_restart_disposition_total",
    "重启时对遗留对账任务的处置（resumed/requeued/interrupted）",
    ["action"], registry=REGISTRY,
)

# ---- 工具面（时延 + 错误码）----
TOOL_REQUESTS = Counter(
    "erp_tool_requests_total",
    "工具调用数（按工具与结果）",
    ["tool", "outcome"], registry=REGISTRY,
)
TOOL_ERRORS = Counter(
    "erp_tool_errors_total",
    "工具失败数（按封闭错误码，不合并为单一错误计数）",
    ["tool", "code"], registry=REGISTRY,
)
TOOL_LATENCY = Histogram(
    "erp_tool_latency_seconds",
    "工具调用时延（秒）",
    ["tool"], registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

_LOG = logging.getLogger("erp.kernel")
_LOG.setLevel(logging.INFO)
_LOG.propagate = False

#: 键名命中即打码（小写子串匹配）。
_SENSITIVE_KEY_HINTS = (
    "password", "passwd", "secret", "token", "credential", "api_key",
    "authorization", "database_url", "dsn", "conninfo", "private_key",
)
#: 值形状像连接串即打码（不依赖键名，防止调用方把 DSN 塞进 detail/msg）。
_DSN_SHAPE = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s]+", re.IGNORECASE)


def _ensure_handler() -> None:
    if any(getattr(h, "_erp_json", False) for h in _LOG.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._erp_json = True  # type: ignore[attr-defined]
    _LOG.addHandler(handler)


def tenant_hash(tenant_id: str | None) -> str | None:
    """租户明文一律不入指标/日志；只留 12 位 sha256 前缀（可关联、不可回读）。"""
    if not tenant_id:
        return None
    return hashlib.sha256(str(tenant_id).encode("utf-8")).hexdigest()[:12]


def _redact_value(value: Any, *, key_hint: str = "") -> Any:
    """递归打码：dict/list 也要走到叶子——detail={"dsn": ...} 不能原样落盘。"""
    if isinstance(value, dict):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(v, key_hint=key_hint) for v in value]
    if isinstance(value, tuple):
        return [_redact_value(v, key_hint=key_hint) for v in value]
    if isinstance(value, str) and _DSN_SHAPE.search(value):
        return "[redacted-dsn]"
    return value


def redact(fields: dict[str, Any]) -> dict[str, Any]:
    """落盘前打码：凭据/连接串/租户明文。返回新 dict，不改调用方对象。

    dict/list 递归到底：只打顶层会漏掉 detail={"dsn": "postgresql://…"} 这类嵌套形态，
    而「日志 MUST NOT 含连接串」不区分层级。
    """
    out: dict[str, Any] = {}
    for key, value in fields.items():
        low = str(key).lower()
        if low == "tenant_id":
            out["tenant_hash"] = tenant_hash(str(value))
            continue
        if any(hint in low for hint in _SENSITIVE_KEY_HINTS):
            out[key] = "[redacted]"
            continue
        out[key] = _redact_value(value, key_hint=low)
    return out


def log_event(event: str, *, tenant_id: str | None = None, **fields: Any) -> dict[str, Any]:
    """写一行结构化 JSON 日志（stdout）；返回写入的 payload 便于测试断言。"""
    _ensure_handler()
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
    }
    th = tenant_hash(tenant_id)
    if th:
        payload["tenant_hash"] = th
    payload.update(redact(fields))
    _LOG.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return payload


def observe_tool(tool: str, *, outcome: str, seconds: float,
                 code: str | None = None, tenant_id: str | None = None) -> None:
    """工具调用一次：时延 + 结果计数 + 错误码分布 + 一行日志。"""
    TOOL_LATENCY.labels(tool=tool).observe(max(seconds, 0.0))
    TOOL_REQUESTS.labels(tool=tool, outcome=outcome).inc()
    if code:
        TOOL_ERRORS.labels(tool=tool, code=code).inc()
    log_event("tool_call", tenant_id=tenant_id, tool=tool, outcome=outcome,
              error_code=code, latency_ms=round(max(seconds, 0.0) * 1000, 3))


def proposal_submitted(kind: str, *, tenant_id: str | None = None) -> None:
    PROPOSALS_SUBMITTED.labels(kind=kind).inc()
    log_event("proposal_submitted", tenant_id=tenant_id, kind=kind)


def proposal_confirmed(*, tenant_id: str | None = None, stable_id: str | None = None) -> None:
    PROPOSALS_CONFIRMED.inc()
    log_event("proposal_confirmed", tenant_id=tenant_id, stable_id=stable_id)


def proposal_rejected(*, tenant_id: str | None = None, stable_id: str | None = None) -> None:
    PROPOSALS_REJECTED.inc()
    log_event("proposal_rejected", tenant_id=tenant_id, stable_id=stable_id)


def fees_billed(path: str, *, currency: str, count: int,
                tenant_id: str | None = None) -> None:
    """path = event | period；按币种分桶（币种是账务口径，不是客户标识）。"""
    FEES_BILLED.labels(path=path, currency=currency).inc(count)
    log_event("fees_billed", tenant_id=tenant_id, path=path,
              currency=currency, fees=count)


def billing_failed(reason: str, *, path: str, tenant_id: str | None = None,
                   detail: dict[str, Any] | None = None) -> None:
    """计费失败按封闭错误码归因（缺规则 / 缺汇率 / 无出处 / 入参…）。"""
    BILLING_FAILURES.labels(reason=reason).inc()
    log_event("billing_failed", tenant_id=tenant_id, path=path,
              error_code=reason, detail=detail or {})


def recon_task_finished(status: str, *, tenant_id: str | None = None,
                        diffs_by_type: dict[str, int] | None = None) -> None:
    RECON_TASKS.labels(status=status).inc()
    for diff_type, count in (diffs_by_type or {}).items():
        if count:
            RECON_DIFFS.labels(diff_type=diff_type).inc(int(count))
    log_event("recon_task_finished", tenant_id=tenant_id, status=status,
              diffs=diffs_by_type or {})


def recon_restart_disposition(action: str, *, task_id: str,
                              reason: str | None = None) -> None:
    RECON_RESTART_DISPOSITION.labels(action=action).inc()
    log_event("recon_restart_disposition", task_id=task_id, action=action,
              reason=reason)


def render_metrics() -> bytes:
    """Prometheus 文本暴露格式（/metrics 端点直接返回）。"""
    return generate_latest(REGISTRY)


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST


def timed() -> float:
    """便于调用方统一口径（monotonic 秒）。"""
    return time.monotonic()
