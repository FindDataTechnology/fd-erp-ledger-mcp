"""分析维度（change: add-ledger-dimensions 任务 1.1）。

维度让账本不只按科目切：同一笔费用可以归到部门/项目/渠道/成本中心/主体。

## 键是封闭集，值是自由字符串

维度**键**是「账本按什么切」，与 `account_type` 同级，属于 schema；维度**值**是客户的组织/项目
编码，属于数据。键封闭不是洁癖：`dept` 与 `department` 并存会让同一部门的实际额被拆成两半，
而报表上看起来只是「数字偏小」——这种错不会报错，只会算错。

新增键要一次迁移：这是有意的摩擦（design D1）。

## 不给就是「未标注」，不是「默认维度」

未传与传空字典等价，都落成空集。绝不回填成某个默认值：猜出来的归属比没有归属更糟，
报表会「看起来完整」而实际是编的（design D5）。
"""
from __future__ import annotations

from typing import Any

from .errors import ErpError

#: 维度键封闭集。加键是一次受审的代码改动（表结构不动，但这个集合是账本的分析 schema），
#: 必须同步本表、文档与消费方。
#:
#: `customer`：应收/收入的主分析轴——「谁欠我、欠多久」与「谁贡献的收入」都靠它
#: （change: add-receivable-aging）。
DIMENSION_KEYS = ("department", "project", "channel", "cost_center", "entity",
                  "customer")

#: 单条提案的维度键上限（一次落账不该按七八个维度切）。
MAX_DIMENSION_KEYS = 6

#: 维度值长度上限（组织/项目编码，不是备注）。
MAX_DIMENSION_VALUE = 64


def normalize_dimensions(value: Any) -> dict[str, str]:
    """校验并规范化维度：未标注 → `{}`；非法即抛，绝不静默丢弃。"""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ErpError(
            "INVALID_ARGUMENT",
            f"dimensions 须为对象（键值对），收到 {type(value).__name__}",
            detail={"allowed": list(DIMENSION_KEYS)},
        )
    unknown = sorted(str(k) for k in value if str(k) not in DIMENSION_KEYS)
    if unknown:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"未知维度键：{', '.join(unknown)}——键是账本 schema，新增需迁移",
            detail={"unknown": unknown, "allowed": list(DIMENSION_KEYS)},
        )
    if len(value) > MAX_DIMENSION_KEYS:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"维度键最多 {MAX_DIMENSION_KEYS} 个，收到 {len(value)} 个",
            detail={"allowed": list(DIMENSION_KEYS), "received": sorted(value)},
        )
    out: dict[str, str] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key)
        if not isinstance(raw_val, str):
            raise ErpError(
                "INVALID_ARGUMENT",
                f"维度值须为字符串：{key}={raw_val!r}",
                detail={"key": key, "value": raw_val},
            )
        val = raw_val.strip()
        if not val:
            raise ErpError(
                "INVALID_ARGUMENT",
                f"维度值不能为空：{key}",
                detail={"key": key, "allowed": list(DIMENSION_KEYS)},
            )
        if len(val) > MAX_DIMENSION_VALUE:
            raise ErpError(
                "INVALID_ARGUMENT",
                f"维度值超长（上限 {MAX_DIMENSION_VALUE}）：{key}",
                detail={"key": key, "length": len(val)},
            )
        out[key] = val
    return out
