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
