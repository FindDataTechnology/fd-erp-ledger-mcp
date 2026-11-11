"""计费全链验收（optimize-erp-solution 5.6 / erp-billing-engine「计费全链验收」）。

一条用例在真实 Postgres 上串起完整链路，允许分段断言但**不许拆成互不相干的用例**
冒充已验收：

    事件计费 → 日终快照计费 → 确认门 → 落账 → 余额 → 快照重算 → 红冲

验收口径（对应 spec 两个 Scenario）：
  * 事件与快照两条路径都生成费用，且确认门前账本零分录；
  * 确认门落账后借贷平衡，余额可由分录确定性复算；
  * 快照重算（get_fee_detail.snapshot_replay）与原明细一致；
  * 红冲经确认门落账后，被冲正那笔费用的净影响归零，余额回到它入账前的口径；
  * 链路断点即整体失败：快照计费因覆盖缺失失败时，费用行 / proposal / 分录
    三项计数都不变（不产生部分落账），修复后重跑得到唯一完整批次。

真库不可 mock：借贷必平是 DEFERRABLE 约束、fee_details 与 journal_entries 都是
append-only，mock 掉就把验收对象本身替换掉了。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import psycopg
import pytest
from fastmcp import Client

from fd_erp_ledger_mcp.billing import (approve_billing_rule, bill_event,
                                       bill_period_snapshots, get_fee_detail,
                                       register_fee_item, reverse_fee,
                                       submit_billing_rule)
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.ledger import compute_balance, query_entries
from fd_erp_ledger_mcp.proposals import confirm_proposal, list_proposals

TENANT = "acme"
PERIOD = "2026-11-09"
FEE_ITEM = "仓储费"
EVENT_KIND = "storage_day"

EVENT_JOURNAL = "ev-1:fee-journal"
PERIOD_JOURNAL = f"bill-period:{PERIOD}:fee-journal"


def _rule_cfg(fee_item: str = FEE_ITEM, event_kind: str = EVENT_KIND,
              unit_price: float = 0.5, **over) -> dict:
    cfg = {"fee_item": fee_item, "event_kind": event_kind, "mode": "unit",
           "unit_price": unit_price, "measure": "quantity",
           "rate_source": "pricebook-2026Q4"}
    cfg.update(over)
    return {k: v for k, v in cfg.items() if v is not None}


def _approve(conn, settings, cfg: dict) -> dict:
    rule = submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=cfg)
    approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                         rule_id=rule["rule_id"], version=rule["version"])
    return rule


def _balances(conn, settings) -> dict[str, Decimal]:
    """余额按科目汇总（compute_balance 按 (科目, 币种) 分行，本位币口径求和）。"""
    out: dict[str, Decimal] = {}
    for row in compute_balance(conn, TENANT, None, settings=settings):
        out[row["account_code"]] = out.get(row["account_code"], Decimal(0)) + \
            Decimal(row["balance"])
    return out


def _sides(conn) -> tuple[list[dict], Decimal, Decimal]:
    """返回 (分录, 借方合计, 贷方合计)；合计按本位币口径。"""
    entries = query_entries(conn, TENANT)
    debit = sum((Decimal(e["amount_base"]) for e in entries
                 if e["direction"] == "debit"), Decimal(0))
    credit = sum((Decimal(e["amount_base"]) for e in entries
                  if e["direction"] == "credit"), Decimal(0))
    return entries, debit, credit


def _counts(settings) -> dict[str, int]:
    """绕开 ORM 直接数三张表：失败不得留下部分费用/部分 proposal/部分分录。"""
    raw = psycopg.connect(settings.database_url, autocommit=True)
    try:
        return {
            "fees": raw.execute("SELECT count(*) FROM fee_details").fetchone()[0],
            "proposals": raw.execute("SELECT count(*) FROM proposals").fetchone()[0],
            "entries": raw.execute("SELECT count(*) FROM journal_entries").fetchone()[0],
            "period_fees": raw.execute(
                "SELECT count(*) FROM fee_details "
                "WHERE event_stable_id LIKE 'bill-period:%'").fetchone()[0],
        }
    finally:
        raw.close()


def test_billing_full_chain_event_period_confirm_balance_replay_reversal(settings):
    """一条真库用例走完「事件计费→快照计费→确认门→落账→余额→重算→红冲」。"""
    with connection(settings) as conn:
        # ---- 0. 目录 + 规则：登记在用收费项，规则审批上线 ----
        register_fee_item(conn, tenant_id=TENANT, fee_item=FEE_ITEM,
                          event_kind=EVENT_KIND, source_ref="pricebook-2026Q4")
        _approve(conn, settings, _rule_cfg(unit_price=0.5))
        assert query_entries(conn, TENANT) == []

        # ---- 1. 事件计费：路径一（event），费用 10 × 0.5 = 5 ----
        event_result = bill_event(conn, settings=settings, tenant_id=TENANT, event={
            "stable_id": "ev-1", "kind": EVENT_KIND, "occurred_at": PERIOD,
            "measures": {"quantity": 10}, "refs": {"document": "WS-1"}})
        assert event_result["duplicate"] is False
        event_fee = event_result["fees"][0]
        assert event_fee["amount"] == "5.000000"
        assert event_fee["event_stable_id"] == "ev-1"

        # ---- 2. 日终快照计费：路径二（period），100 × 0.5 = 50 ----
        period_result = bill_period_snapshots(
            conn, settings=settings, tenant_id=TENANT, period=PERIOD,
            snapshots=[{"fee_item": FEE_ITEM, "as_of": PERIOD,
                        "measures": {"quantity": 100}}],
            source_ref="eod-2026-11-09")
        assert period_result["duplicate"] is False
        period_fee = period_result["fees"][0]
        assert period_fee["amount"] == "50.000000"

        # 两条路径都留下了可归因的机器车道 proposal；确认门前账本零分录。
        pending = {p["stable_id"]: p["status"] for p in list_proposals(conn, TENANT)}
        assert pending == {EVENT_JOURNAL: "pending", PERIOD_JOURNAL: "pending"}
        entries, debit, credit = _sides(conn)
        assert entries == [] and debit == credit == Decimal(0)

        # 同期间重跑幂等：不重复计费，返回首次费用行。
        again = bill_period_snapshots(
            conn, settings=settings, tenant_id=TENANT, period=PERIOD,
            snapshots=[{"fee_item": FEE_ITEM, "as_of": PERIOD,
                        "measures": {"quantity": 100}}])
        assert again["duplicate"] is True
        assert [f["fee_id"] for f in again["fees"]] == [period_fee["fee_id"]]

        # ---- 3. 确认门（拟稿车道的落账入口）：先事件后快照 ----
        event_confirm = confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                                         stable_id=EVENT_JOURNAL)
        assert event_confirm["status"] == "confirmed"
        entries, debit, credit = _sides(conn)
        assert len(entries) == 2 and debit == credit == Decimal("5.000000")
        pre_period = _balances(conn, settings)          # 被冲正那笔入账前的口径
        assert pre_period == {"1300": Decimal("5.000000"), "4000": Decimal("-5.000000")}

        period_confirm = confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                                          stable_id=PERIOD_JOURNAL)
        assert period_confirm["status"] == "confirmed"

        # ---- 4. 落账 + 余额：借贷平衡，余额可由分录复算 ----
        entries, debit, credit = _sides(conn)
        assert len(entries) == 4
        assert debit == credit == Decimal("55.000000")   # 5 + 50，两侧同额
        balances = _balances(conn, settings)
        assert balances == {"1300": Decimal("55.000000"), "4000": Decimal("-55.000000")}
        assert sum(balances.values()) == Decimal(0)      # 复式账净额为零
        # 余额确定性复算：直接对分录重算，与 compute_balance 口径一致。
        assert sum(Decimal(e["amount_base"]) for e in entries
                   if e["account_code"] == "1300") == balances["1300"]

        # ---- 5. 快照重算：与原明细一致（两条路径都验） ----
        for fee, expected in ((event_fee, "5.000000"), (period_fee, "50.000000")):
            detail = get_fee_detail(conn, settings=settings, tenant_id=TENANT,
                                    fee_id=fee["fee_id"])
            replay = detail["snapshot_replay"]
            assert replay["matches"] is True
            assert replay["base_matches"] is True
            assert replay["amount"] == expected == detail["fee"]["amount"]
            assert detail["journal"]["txn_id"] is not None
        period_detail = get_fee_detail(conn, settings=settings, tenant_id=TENANT,
                                       fee_id=period_fee["fee_id"])
        assert period_detail["journal"]["txn_id"] == period_confirm["txn_id"]

        # ---- 6. 红冲：反向费用行 + 冲正 proposal，落账后净影响归零 ----
        reversal = reverse_fee(conn, settings=settings, tenant_id=TENANT,
                               fee_id=period_fee["fee_id"], reason="期间库存快照口径调整")
        assert reversal["journal_proposal_id"] is not None
        assert reversal["idempotent_hit"] is False
        # 冲正 proposal 未确认前账本不变（仍是 4 行），说明红冲也走确认门。
        entries, _debit, _credit = _sides(conn)
        assert len(entries) == 4

        rev_confirm = confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                                       proposal_id=reversal["journal_proposal_id"])
        assert rev_confirm["status"] == "confirmed"

        entries, debit, credit = _sides(conn)
        assert len(entries) == 6
        assert debit == credit == Decimal("105.000000")  # 借：5+50+50，贷：5+50+50
        # 被冲正费用（50）的净影响归零 → 余额回到它入账前的口径 pre_period。
        assert _balances(conn, settings) == pre_period
        rev_entries = [e for e in entries if e["is_reversal"]]
        assert len(rev_entries) == 2
        assert {e["reversal_of"] for e in rev_entries} == {period_confirm["txn_id"]}

        # 原费用行保持原样（append-only），反向行 status='reversed' 且带 reversal_of。
        after = get_fee_detail(conn, settings=settings, tenant_id=TENANT,
                               fee_id=period_fee["fee_id"])
        assert after["fee"]["status"] == "normal"
        assert after["is_reversed"] is True
        assert [r["id"] for r in after["reversals"]] == [reversal["reversal_fee_id"]]
        rev_detail = get_fee_detail(conn, settings=settings, tenant_id=TENANT,
                                    fee_id=reversal["reversal_fee_id"])
        assert rev_detail["fee"]["status"] == "reversed"
        assert rev_detail["fee"]["reversal_of"] == period_fee["fee_id"]
        assert rev_detail["snapshot_replay"]["matches"] is True

        # 全链终态：三条 proposal 全部确认，账面无悬挂待确认批次。
        final = {p["stable_id"]: p["status"] for p in list_proposals(conn, TENANT)}
        assert set(final.values()) == {"confirmed"}
        assert len(final) == 3


def test_period_billing_tool_entry_produces_pending_batch(settings):
    """排程入口：MCP 工具 `bill_period_snapshot` 产出待确认批次，未确认前不落账。

    对应 spec「排程触发产出待确认批次」：库内函数不是唯一入口，工具面必须真的可调。
    """
    from fd_erp_ledger_mcp.server import create_server

    with connection(settings) as conn:
        register_fee_item(conn, tenant_id=TENANT, fee_item=FEE_ITEM,
                          event_kind=EVENT_KIND)
        _approve(conn, settings, _rule_cfg())

    server = create_server(settings)

    async def go():
        async with Client(server) as client:
            return await client.call_tool("bill_period_snapshot", {
                "period": PERIOD,
                "snapshots": [{"fee_item": FEE_ITEM, "as_of": PERIOD,
                               "measures": {"quantity": 100}}],
                "source_ref": "eod-2026-11-09",
            })

    result = asyncio.run(go())
    data = result.data if hasattr(result, "data") else result
    assert data["ok"] is True
    assert data["duplicate"] is False
    assert data["fees"][0]["amount"] == "50.000000"

    with connection(settings) as conn:
        # 排程只出待确认批次：确认门前账本零分录。
        assert query_entries(conn, TENANT) == []
        pending = [p for p in list_proposals(conn, TENANT) if p["kind"] == "journal"]
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"
        assert pending[0]["source_ref"] == "eod-2026-11-09"
        # 同一确认门确认后落账（2 行：借 1300 / 贷 4000）。
        confirmed = confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                                     stable_id=pending[0]["stable_id"])
        assert confirmed["status"] == "confirmed"
        entries, debit, credit = _sides(conn)
        assert len(entries) == 2
        assert debit == credit == Decimal("50.000000")


def test_full_chain_fails_atomically_when_period_step_has_no_active_rule(settings):
    """反例：快照计费因覆盖缺失失败 → 整体失败，不产生部分落账；修复后重跑唯一成批。"""
    with connection(settings) as conn:
        register_fee_item(conn, tenant_id=TENANT, fee_item=FEE_ITEM,
                          event_kind=EVENT_KIND)
        _approve(conn, settings, _rule_cfg())
        # 前置步骤（事件计费 + 确认）先真实跑通，证明失败不是「全空」而是「断点」。
        event_result = bill_event(conn, settings=settings, tenant_id=TENANT, event={
            "stable_id": "ev-broken", "kind": EVENT_KIND, "occurred_at": "2026-11-10",
            "measures": {"quantity": 2}})
        assert event_result["fees"][0]["amount"] == "1.000000"
        confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                         stable_id="ev-broken:fee-journal")
        before = _counts(settings)
        before_entries, before_debit, before_credit = _sides(conn)
        assert before == {"fees": 1, "proposals": 1, "entries": 2, "period_fees": 0}
        assert before_debit == before_credit == Decimal("1.000000")

        # 断点：登记在用的第二收费项没有 active 规则 → 快照计费整批拒绝。
        register_fee_item(conn, tenant_id=TENANT, fee_item="搬运费",
                          event_kind="handling")
        with pytest.raises(ErpError) as ei:
            bill_period_snapshots(
                conn, settings=settings, tenant_id=TENANT, period="2026-11-10",
                snapshots=[{"fee_item": FEE_ITEM, "as_of": "2026-11-10",
                            "measures": {"quantity": 100}}])
        assert ei.value.code == "FEE_COVERAGE_MISSING"
        assert ei.value.detail["stage"] == "period_billing"
        assert ei.value.detail["period"] == "2026-11-10"
        assert ei.value.detail["fee_items"] == ["搬运费"]

        # 整体失败：费用行 / proposal / 分录三项计数与失败前完全一致，无部分批次。
        after = _counts(settings)
        assert after == before
        assert after["period_fees"] == 0
        after_entries, after_debit, after_credit = _sides(conn)
        assert after_entries == before_entries
        assert (after_debit, after_credit) == (before_debit, before_credit)
        assert {p["stable_id"] for p in list_proposals(conn, TENANT)} == \
            {"ev-broken:fee-journal"}

        # 修复覆盖（补规则上线）后重跑同一期间：得到唯一完整批次，不是半个批次。
        _approve(conn, settings, _rule_cfg(fee_item="搬运费", event_kind="handling",
                                           unit_price=1))
        retried = bill_period_snapshots(
            conn, settings=settings, tenant_id=TENANT, period="2026-11-10",
            snapshots=[{"fee_item": FEE_ITEM, "as_of": "2026-11-10",
                        "measures": {"quantity": 100}}])
        assert retried["duplicate"] is False
        assert len(retried["fees"]) == 1
        assert retried["fees"][0]["amount"] == "50.000000"
        fixed = _counts(settings)
        assert fixed == {"fees": 2, "proposals": 2, "entries": 2, "period_fees": 1}
