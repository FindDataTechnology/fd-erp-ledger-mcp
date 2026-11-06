"""计量接入（wire-facade-metering）：rows-returned 落戳口径对齐。

按行计费以工具响应中的 rows_returned 元数据为唯一依据。本内核的责任是：
每个工具响应都带齐该戳；写工具（proposal/确认/拒绝）落 0，表示「调用入账、
行数不可计」，绝不以字段缺失（null）代替 0。wire 入口侧的额度闸/审计事件
由 registry 承担，本模块只保证响应侧口径，不臆造计费动作。
"""
from __future__ import annotations

from .result import stamp  # noqa: F401  (单一实现，避免两处漂移)


def assert_stamped(result: dict) -> None:
    """selfcheck 用：成功响应必须有 rows_returned 戳。"""
    if "rows_returned" not in result:
        raise AssertionError("tool response missing rows_returned stamp")
