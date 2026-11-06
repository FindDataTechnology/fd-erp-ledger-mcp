"""契约单测（2.4 / 3.3）：错误码封闭集合、rows_returned 落戳、结果截断续读。"""
from __future__ import annotations

from fd_erp_ledger_mcp.error_codes import CATALOG, REQUIRED_CLASSES, assert_closed_catalog
from fd_erp_ledger_mcp.result import RESULT_MAX_ROWS, paginate, stamp


def test_error_catalog_covers_four_classes():
    assert_closed_catalog()
    classes = {e.semantic_class for e in CATALOG.values()}
    assert REQUIRED_CLASSES <= classes


def test_every_error_code_retryable_flag_distinguishes_classes():
    # 上游类可重试，入参/未接线不可重试 —— 可观测差异
    assert CATALOG["UPSTREAM_UNAVAILABLE"].retryable is True
    assert CATALOG["INVALID_ARGUMENT"].retryable is False
    assert CATALOG["NOT_WIRED"].retryable is False
    assert CATALOG["NOT_FOUND"].retryable is False


def test_unregistered_code_rejected():
    from fd_erp_ledger_mcp.error_codes import lookup
    try:
        lookup("NOT_A_REAL_CODE")
    except KeyError:
        return
    raise AssertionError("未登记错误码应抛 KeyError")


def test_stamp_always_present_writes_zero():
    assert stamp({}, rows=0)["rows_returned"] == 0
    assert stamp({"data": [1, 2, 3]})["rows_returned"] == 3
    # 以缺戳区分「行数不可计」是违规的：stamp 永远写整数
    assert isinstance(stamp({}, rows=0)["rows_returned"], int)


def test_paginate_marks_truncation_and_continuation():
    rows = [{"i": i} for i in range(RESULT_MAX_ROWS + 20)]
    page = paginate(rows, offset=0, limit=RESULT_MAX_ROWS + 20)
    assert page["truncated"] is True
    assert page["total"] == RESULT_MAX_ROWS + 20
    assert page["continuation"]["next_offset"] == len(page["data"])
    # 续读拼回完整、不丢不重
    rest = paginate(rows, offset=page["continuation"]["next_offset"], limit=RESULT_MAX_ROWS)
    assert [r["i"] for r in page["data"] + rest["data"]] == list(range(len(rows)))


def test_paginate_rejects_negative():
    from fd_erp_ledger_mcp.errors import ErpError
    try:
        paginate([], offset=-1, limit=10)
    except ErpError as exc:
        assert exc.code == "INVALID_ARGUMENT"
        return
    raise AssertionError("负 offset 应报 INVALID_ARGUMENT")
