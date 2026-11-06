"""工具装饰器：错误映射 + 计量落戳 + 统一结果契约。"""
from __future__ import annotations

import functools
from typing import Any, Callable

from .errors import ErpError, error_envelope, map_psycog_error
from .result import stamp


def safe_tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """把领域/数据库异常翻成机器可读错误码；成功响应统一落 rows_returned 戳。

    - 失败绝不返回空结果伪装成功（error_envelope 带 code）。
    - 数据库约束异常经 map_psycog_error 归入封闭码（UNBALANCED / APPEND_ONLY / ...）。
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return stamp(fn(*args, **kwargs))
        except ErpError as exc:
            return error_envelope(exc)
        except Exception as exc:  # psycopg / httpx / 其他
            return error_envelope(map_psycog_error(exc))
    return wrapper
