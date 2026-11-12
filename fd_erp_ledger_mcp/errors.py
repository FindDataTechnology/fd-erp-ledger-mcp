"""错误对象与映射（mcp-error-semantics / mcp-result-contract）。"""
from __future__ import annotations

from typing import Any

from .error_codes import lookup


class ErpError(Exception):
    """带封闭错误码的领域异常。"""

    def __init__(self, code: str, message: str, *, hint: str | None = None,
                 detail: dict[str, Any] | None = None):
        ec = lookup(code)
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint or ec.default_hint
        self.retryable = ec.retryable
        self.detail = detail or {}

    def to_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
            **({"detail": self.detail} if self.detail else {}),
        }


def map_psycog_error(exc: Exception) -> ErpError:
    """把数据库约束/连接异常映射为封闭码，绝不伪装成空成功。"""
    text = str(exc)
    # SQLSTATE 23514 = check_violation；我们在约束消息里埋了稳定前缀。
    # 事件层的专属前缀必须排在 ERP_APPEND_ONLY 之前：两者都含 append-only 语义，
    # 但事件层的处置不同（改定型事件=提交冲正；改分录=走冲正分录），混淆会把
    # 调用方指向错误的修法。
    if "ERP_EVENT_IMMUTABLE" in text:
        return ErpError("EVENT_IMMUTABLE", f"数据库拒绝改写事件：{text}")
    if "ERP_OBJECT_CONFLICT" in text:
        return ErpError("OBJECT_CONFLICT", f"数据库拒绝对象投影写入：{text}")
    # 多板块生产者（0011）：注册侧闸抛的码必须在这里有匹配，否则会塌成 UPSTREAM_UNAVAILABLE
    # ——调用方会以为上游抖了一下，而真相是「这个类型不许登记」。
    if "ERP_EVENT_FABRICATED_FACT" in text:
        return ErpError("EVENT_FABRICATED_FACT", f"数据库拒绝登记无载体的事件类型：{text}")
    if "ERP_EVENT_PRODUCER_MISMATCH" in text:
        return ErpError("EVENT_PRODUCER_MISMATCH", f"数据库拒绝板块与类型名不自洽的登记：{text}")
    if "ERP_EVENT_UNKNOWN_TYPE" in text:
        return ErpError("EVENT_UNKNOWN_TYPE", f"事件类型未登记：{text}")
    if "ERP_UNBALANCED" in text or "check_violation" in text and "balance" in text.lower():
        return ErpError("UNBALANCED", f"数据库拒绝不平分录：{text}")
    if "ERP_APPEND_ONLY" in text or "append_only" in text.lower():
        return ErpError("APPEND_ONLY", f"数据库拒绝改/删已落账分录：{text}")
    if "ERP_FX_UNSOURCED" in text or "erp_fx_unsourced" in text:
        return ErpError("FX_UNSOURCED", f"数据库拒绝无出处汇率分录：{text}")
    if "ERP_TENANT" in text:
        return ErpError("TENANT_DENIED", f"数据库拒绝跨租户写入：{text}")
    if "unique_violation" in text or "23505" in text:
        return ErpError("INTERNAL", f"唯一约束冲突（应由幂等路径处理）：{text}")
    # 连接/上游类
    if any(k in type(exc).__name__.lower() for k in ("operational", "interface", "pool", "connection")):
        return ErpError("UPSTREAM_UNAVAILABLE", f"数据库上游不可用：{text}")
    return ErpError("UPSTREAM_UNAVAILABLE", f"数据库错误：{text}")


def error_envelope(exc: ErpError) -> dict[str, Any]:
    """失败响应：机器可读码 + 可读描述 + 修正建议；绝不返回空列表冒充成功。"""
    return {"ok": False, "error": exc.to_error(), "rows_returned": 0}
