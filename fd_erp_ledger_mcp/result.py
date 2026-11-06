"""结果契约 + 计量落戳（mcp-result-contract / wire-facade-metering）。

- 单次响应统一体积上限 RESULT_MAX_ROWS；超限返回截断标记 + 原始规模 + 分页续读。
- 每次成功工具响应带 rows_returned 戳（按行计费唯一依据）；流式/写成功不可计行数时落 0，
  以缺戳（字段缺失）区分「调用入账、行数不可计」。
"""
from __future__ import annotations

from typing import Any

RESULT_MAX_ROWS = 500


def stamp(result: dict[str, Any], rows: int | None = None) -> dict[str, Any]:
    """给成功响应补 ok / rows_returned；rows=None 时以 data 长度计，写工具显式传 0。"""
    result.setdefault("ok", True)
    if rows is None:
        data = result.get("data")
        rows = len(data) if isinstance(data, list) else 0
    result["rows_returned"] = int(rows)
    return result


def paginate(rows: list[dict[str, Any]], *, offset: int, limit: int) -> dict[str, Any]:
    """统一上限 + 截断标记 + 原始规模 + 续读方式；不静默截断、不放开上限。"""
    if offset < 0 or limit < 0:
        from .errors import ErpError
        raise ErpError("INVALID_ARGUMENT", "offset/limit 必须为非负整数",
                       detail={"offset": offset, "limit": limit})
    cap = min(limit, RESULT_MAX_ROWS) if limit else RESULT_MAX_ROWS
    window = rows[offset:offset + cap]
    total = len(rows)
    out: dict[str, Any] = {
        "data": window,
        "total": total,
        "offset": offset,
        "limit": cap,
        "truncated": offset + len(window) < total or total > RESULT_MAX_ROWS,
    }
    if out["truncated"]:
        out["continuation"] = {"method": "offset", "next_offset": offset + len(window)}
    return out
