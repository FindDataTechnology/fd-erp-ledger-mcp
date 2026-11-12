"""分析维度（change: add-ledger-dimensions 任务 1.1-1.6）。

判据（对应 spec `erp-analytical-dimensions`）：

1. 维度键封闭、值自由：未知键/空值/非字符串/非字典/超上限一律拒并点名；
2. 确认后每一条分录都带提案维度；
3. 冲正继承原分录维度（红字不许跑到别的维度）；
4. 查询可按维度过滤（多键为「与」）并回显；
5. 不带维度照常落账，且维度不改幂等与借贷必平。

真库（DB 约束层不可 mock）。
"""
from __future__ import annotations

import pytest

from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.dimensions import DIMENSION_KEYS, normalize_dimensions
from fd_erp_ledger_mcp.ledger import query_entries
from fd_erp_ledger_mcp.proposals import (confirm_proposal, submit_proposal)
from fd_erp_ledger_mcp.db import connection


def _lines(amount="100", debit="5200", credit="1000"):
    return [
        {"account_code": debit, "direction": "debit", "amount": amount,
         "currency": "CNY"},
        {"account_code": credit, "direction": "credit", "amount": amount,
         "currency": "CNY"},
    ]


def _submit(s, stable_id, **kw):
    with connection(s) as conn:
        return submit_proposal(conn, settings=s, tenant_id=s.tenant_id,
                               stable_id=stable_id, lines=_lines(), **kw)


def _confirm(s, stable_id):
    with connection(s) as conn:
        return confirm_proposal(conn, settings=s, tenant_id=s.tenant_id,
                                stable_id=stable_id, confirmed_by="owner")


def _entries(s, **kw):
    with connection(s) as conn:
        return query_entries(conn, s.tenant_id, **kw)


# ---- 1) 校验 ----

def test_normalize_accepts_the_closed_set():
    assert normalize_dimensions({"department": " D-1 ", "channel": "online"}) == {
        "department": "D-1", "channel": "online"}
    assert normalize_dimensions(None) == {}
    assert normalize_dimensions({}) == {}


@pytest.mark.parametrize("bad,needle", [
    ({"dept": "D-1"}, "未知维度键"),
    ({"department": "  "}, "不能为空"),
    ({"department": 1}, "须为字符串"),
    (["department"], "须为对象"),
    ({"department": "X" * 80}, "超长"),
    ({k: "v" for k in list(DIMENSION_KEYS) + ["extra"]}, "未知维度键"),
])
def test_normalize_rejects_bad_shape(bad, needle):
    with pytest.raises(ErpError) as ei:
        normalize_dimensions(bad)
    assert ei.value.code == "INVALID_ARGUMENT"
    assert needle in ei.value.message


def test_unknown_key_lists_allowed_set():
    with pytest.raises(ErpError) as ei:
        normalize_dimensions({"dept": "D-1"})
    assert ei.value.detail["allowed"] == list(DIMENSION_KEYS)


def test_submit_rejects_unknown_dimension_before_idempotency(settings):
    """非法维度不因为「同 stable_id 已存在」被悄悄放过。"""
    _submit(settings, "dim-bad-1")
    with pytest.raises(ErpError) as ei:
        _submit(settings, "dim-bad-1", dimensions={"dept": "D-1"})
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "未知维度键" in ei.value.message


# ---- 2) 落账携带 ----

def test_confirmed_entries_carry_dimensions(settings):
    _submit(settings, "dim-ok-1", dimensions={"department": "D-1",
                                              "channel": "online"})
    _confirm(settings, "dim-ok-1")
    rows = _entries(settings, account_code="5200")
    assert len(rows) == 1
    assert rows[0]["dimensions"] == {"department": "D-1", "channel": "online"}


def test_proposal_view_echoes_dimensions(settings):
    out = _submit(settings, "dim-view-1", dimensions={"project": "P-9"})
    assert out["dimensions"] == {"project": "P-9"}
    # 不传维度：回显空集，不是 None
    assert _submit(settings, "dim-view-2")["dimensions"] == {}


def test_no_dimensions_still_posts(settings):
    _submit(settings, "dim-none-1")
    _confirm(settings, "dim-none-1")
    rows = _entries(settings, account_code="5200")
    assert len(rows) == 1
    assert rows[0]["dimensions"] == {}


def test_dimension_does_not_change_idempotency(settings):
    _submit(settings, "dim-idem-1", dimensions={"department": "D-1"})
    again = _submit(settings, "dim-idem-1", dimensions={"department": "D-2"})
    assert again["duplicate"] is True
    assert again["dimensions"] == {"department": "D-1"}   # 首次为准
    _confirm(settings, "dim-idem-1")
    rows = _entries(settings, account_code="5200")
    assert len(rows) == 1 and rows[0]["dimensions"] == {"department": "D-1"}


# ---- 3) 冲正继承 ----

def test_reversal_inherits_dimensions(settings):
    _submit(settings, "dim-rev-src", dimensions={"project": "P-9"})
    first = _confirm(settings, "dim-rev-src")
    with connection(settings) as conn:
        submit_proposal(conn, settings=settings, tenant_id=settings.tenant_id,
                        stable_id="dim-rev-1", lines=[], kind="reversal",
                        reversal_of=first["txn_id"])
    _confirm(settings, "dim-rev-1")
    rows = _entries(settings, account_code="5200")
    assert len(rows) == 2
    assert all(r["dimensions"] == {"project": "P-9"} for r in rows)
    net = sum(float(r["amount_base"]) if r["direction"] == "debit"
              else -float(r["amount_base"]) for r in rows)
    assert abs(net) < 1e-9        # 同维度净额归零


# ---- 4) 查询过滤 ----

def test_query_filter_is_an_intersection(settings):
    _submit(settings, "dim-f-1", dimensions={"department": "D-1", "channel": "online"})
    _submit(settings, "dim-f-2", dimensions={"department": "D-1", "channel": "offline"})
    _submit(settings, "dim-f-3", dimensions={"department": "D-2", "channel": "online"})
    _submit(settings, "dim-f-4")
    for sid in ("dim-f-1", "dim-f-2", "dim-f-3", "dim-f-4"):
        _confirm(settings, sid)

    only_dept = _entries(settings, account_code="5200", dimensions={"department": "D-1"})
    assert len(only_dept) == 2
    both = _entries(settings, account_code="5200",
                    dimensions={"department": "D-1", "channel": "online"})
    assert len(both) == 1
    # 未标注维度的分录不出现在任何维度过滤里
    assert all(r["dimensions"] for r in only_dept)
    # 不过滤时四条都在
    assert len(_entries(settings, account_code="5200")) == 4


def test_balance_semantics_unchanged_by_dimensions(settings):
    """余额仍按科目重算：维度不参与余额口径（按维度汇总由调用方做）。"""
    from fd_erp_ledger_mcp.ledger import compute_balance
    _submit(settings, "dim-bal-1", dimensions={"department": "D-1"})
    _submit(settings, "dim-bal-2")
    _confirm(settings, "dim-bal-1")
    _confirm(settings, "dim-bal-2")
    with connection(settings) as conn:
        rows = compute_balance(conn, settings.tenant_id, "5200", settings=settings)
    assert rows[0]["debit_total"] == "200.000000"
