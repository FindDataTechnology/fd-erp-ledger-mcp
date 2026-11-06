"""逐笔对齐 + 四类差异（任务 4.2）。

差异四类：missing_in_right（缺失）/ extra_in_right（多余）/
amount_mismatch（金额不符）/ status_mismatch（状态不符）。
每条差异 SHALL 含双侧原始引用（left_ref / right_ref）+ 双侧原始记录快照，
报告落档可查，且显著强于 Excel 人工对账的可解释性。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .adapters import SourceRecord

DIFF_TYPES = ("missing_in_right", "extra_in_right", "amount_mismatch", "status_mismatch")


def _dedup(records: list[SourceRecord]) -> tuple[dict[str, SourceRecord], list[dict[str, Any]]]:
    """stable_id 幂等去重：重推（同 stable_id）只保留首次，重复次数留痕。"""
    out: dict[str, SourceRecord] = {}
    replays: list[dict[str, Any]] = []
    for r in records:
        if r.stable_id in out:
            replays.append({"stable_id": r.stable_id,
                            "duplicate_source_ref": r.source_ref})
        else:
            out[r.stable_id] = r
    return out, replays


def align(left: list[SourceRecord], right: list[SourceRecord]) -> dict[str, Any]:
    """对齐两源流水，产出差异清单 + 对账计数。纯函数，零写入。"""
    left_map, left_replays = _dedup(left)
    right_map, right_replays = _dedup(right)
    diffs: list[dict[str, Any]] = []
    matched = 0

    for sid in sorted(set(left_map) | set(right_map)):
        l = left_map.get(sid)
        r = right_map.get(sid)
        base = {
            "stable_id": sid,
            "left_ref": l.source_ref if l else None,
            "right_ref": r.source_ref if r else None,
            "left_raw": l.raw if l else None,
            "right_raw": r.raw if r else None,
        }
        if l and not r:
            diffs.append({**base, "diff_type": "missing_in_right",
                          "amount": str(l.amount), "currency": l.currency,
                          "status": l.status,
                          "explanation": "左源有、右源无（右源缺失该笔）"})
        elif r and not l:
            diffs.append({**base, "diff_type": "extra_in_right",
                          "amount": str(r.amount), "currency": r.currency,
                          "status": r.status,
                          "explanation": "右源有、左源无（右源多出该笔）"})
        else:
            assert l is not None and r is not None
            if l.amount != r.amount:
                diffs.append({**base, "diff_type": "amount_mismatch",
                              "left_amount": str(l.amount), "right_amount": str(r.amount),
                              "currency": l.currency,
                              "explanation": "两侧金额不符"})
            if l.status != r.status:
                diffs.append({**base, "diff_type": "status_mismatch",
                              "left_status": l.status, "right_status": r.status,
                              "explanation": "两侧状态不符"})
            if l.amount == r.amount and l.status == r.status and l.currency == r.currency:
                matched += 1

    counts = {t: sum(1 for d in diffs if d["diff_type"] == t) for t in DIFF_TYPES}
    return {
        "diffs": diffs,
        "summary": {
            "left_unique": len(left_map),
            "right_unique": len(right_map),
            "matched": matched,
            "diff_count": len(diffs),
            "by_type": counts,
            "replays_deduped": left_replays + right_replays,
            "external_writes": 0,  # 只读滩头：实证阶段对外部系统零写入
        },
    }
