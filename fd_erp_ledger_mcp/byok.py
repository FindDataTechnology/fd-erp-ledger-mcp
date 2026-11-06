"""BYOK 凭证注入面（erp-board / crm-board 契约）。

凭证只经环境变量注入：零落库、零日志。本模块刻意不提供任何持久化 API，
也不把明文放进异常/日志/返回值。验证用例见 tests/test_byok.py。
"""
from __future__ import annotations

import os

BYOK_PREFIX = "ERP_BYOK_"


def resolve_credential(name: str) -> str | None:
    """按名解析 `ERP_BYOK_<NAME>` 环境变量；无则 None（调用方应如实报错）。

    name 仅允许 [A-Za-z0-9_]，防止路径/注入式变量名。
    """
    if not name or not all(c.isalnum() or c == "_" for c in name):
        from .errors import ErpError
        raise ErpError("INVALID_ARGUMENT", "凭证名非法（仅允许字母数字下划线）",
                       detail={"name": name})
    return os.environ.get(BYOK_PREFIX + name.upper())


def redact(text: str, secret: str | None) -> str:
    """日志脱敏：即便误把凭证拼进文本也不外泄。"""
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text


def credential_policy() -> dict:
    """供文档/selfcheck 引用的口径声明（非凭证本身）。"""
    return {
        "injection": "environment-variable-only",
        "prefix": BYOK_PREFIX,
        "persisted_to_db": False,
        "written_to_logs": False,
        "note": "执行面仅采纳外部 MCP；平台不代持任何客户凭证。",
    }
