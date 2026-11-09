"""工具装饰器：错误映射 + 计量落戳 + 运行指标 + 统一结果契约。"""
from __future__ import annotations

import functools
import time
from typing import Any, Callable

from . import observability as obs
from .errors import ErpError, error_envelope, map_psycog_error
from .result import stamp


def safe_tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """把领域/数据库异常翻成机器可读错误码；成功响应统一落 rows_returned 戳。

    - 失败绝不返回空结果伪装成功（error_envelope 带 code）。
    - 数据库约束异常经 map_psycog_error 归入封闭码（UNBALANCED / APPEND_ONLY / ...）。
    - 每次调用落运行指标（时延 histogram / 结果计数 / 错误码分布）与一行 JSON 日志
      （optimize-erp-solution 6.1）；租户只以哈希出现，日志经 observability.redact。
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started = time.monotonic()
        tool = fn.__name__
        tenant = kwargs.get("tenant_id")
        try:
            result = stamp(fn(*args, **kwargs))
            obs.observe_tool(tool, outcome="ok",
                             seconds=time.monotonic() - started, tenant_id=tenant)
            return result
        except ErpError as exc:
            obs.observe_tool(tool, outcome="error", code=exc.code,
                             seconds=time.monotonic() - started, tenant_id=tenant)
            return error_envelope(exc)
        except Exception as exc:  # psycopg / httpx / 其他
            err = map_psycog_error(exc)
            obs.observe_tool(tool, outcome="error", code=err.code,
                             seconds=time.monotonic() - started, tenant_id=tenant)
            return error_envelope(err)
    return wrapper
