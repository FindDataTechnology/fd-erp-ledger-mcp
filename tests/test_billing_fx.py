"""计费多币种折算（optimize-erp-solution 5.1/5.2 / erp-billing-engine「计费多币种折算」）。

验收口径：
  * 外币费用落账同时带原币金额、本位币金额、汇率、汇率时点与出处（四要素）；
  * 没登记汇率 → 拒绝且账本零分录、fee_details 零行、proposal 零条（不留半批）；
  * 与登记不符的汇率在账本层被拒（fx_facts 复核）；
  * 汇率出处缺失（规则与默认源都没有）→ 拒绝，不猜汇率；
  * 同批多币种费用按币种分组行，借贷仍平。
全部对真实 Postgres 跑（fx_facts 出处闸与 append-only 不可 mock）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fd_erp_ledger_mcp.billing import (approve_billing_rule, bill_event,
                                       get_fee_detail, reverse_fee,
                                       submit_billing_rule)
from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.fx import seed_fx_facts
from fd_erp_ledger_mcp.ledger import compute_balance, query_entries
from fd_erp_ledger_mcp.proposals import confirm_proposal, list_proposals, submit_proposal

TENANT = "acme"
AS_OF = "2026-11-09"


def _usd_rule(**over):
    cfg = {
        "fee_item": "跨境服务费",
        "event_kind": "crossborder_order",
        "mode": "unit",
        "unit_price": 2,
        "measure": "quantity",
        "rate_source": "pricebook-2026Q4",
        "currency": "USD",
    }
    cfg.update(over)
    return {k: v for k, v in cfg.items() if v is not None}


def _usd_event(stable="cb-1", qty=10):
    return {"stable_id": stable, "kind": "crossborder_order", "occurred_at": AS_OF,
            "measures": {"quantity": qty}, "refs": {"document": "SO-USD-1"}}


def _balances(conn, settings) -> dict[str, Decimal]:
    """余额按科目汇总（compute_balance 按 (科目, 币种) 分行，本位币口径求和）。"""
    out: dict[str, Decimal] = {}
    for row in compute_balance(conn, TENANT, None, settings=settings):
        out[row["account_code"]] = out.get(row["account_code"], Decimal(0)) + \
            Decimal(row["balance"])
    return out


def _approve(conn, settings, cfg):
    r = submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=cfg)
    approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                         rule_id=r["rule_id"], version=r["version"])
    return r


def test_foreign_currency_fee_posts_with_original_base_rate_and_source(settings):
    with connection(settings) as conn:
        seed_fx_facts(conn, [{"quote_currency": "USD", "rate": "7.0", "as_of": AS_OF,
                              "source": "ecb-exr"}], settings=settings)
        _approve(conn, settings, _usd_rule())

        res = bill_event(conn, settings=settings, tenant_id=TENANT, event=_usd_event(qty=10))
        assert res["duplicate"] is False
        fee = res["fees"][0]
        # 费用明细自带四要素（可独立审计，不必翻账本）
        assert fee["amount"] == "20.000000"          # 原币
        assert fee["original_amount"] == "20.000000"
        assert fee["base_amount"] == "140.000000"    # 20 × 7.0
        assert fee["fx_rate"] == "7.0000000000"
        assert fee["fx_as_of"] == AS_OF
        assert fee["fx_source"] == "ecb-exr"
        # 确认门前不落账
        assert query_entries(conn, TENANT) == []

        confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                         stable_id="cb-1:fee-journal")
        entries = query_entries(conn, TENANT)
        assert len(entries) == 2
        for e in entries:
            assert e["currency"] == "USD"
            assert e["amount"] == "20.000000"        # 原币金额
            assert e["amount_base"] == "140.000000"  # 本位币金额
            assert e["fx_rate"] == "7.0000000000"
            assert e["fx_as_of"] == AS_OF
            assert e["fx_source"] == "ecb-exr"

        bal = _balances(conn, settings)
        assert bal["1300"] == Decimal("140.000000")
        assert bal["4000"] == Decimal("-140.000000")  # 贷方余额为负（借-贷）

        detail = get_fee_detail(conn, settings=settings, tenant_id=TENANT,
                                fee_id=fee["fee_id"])
        assert detail["fx"] == {"currency": "USD", "original_amount": "20.000000",
                                "base_amount": "140.000000", "rate": "7.0000000000",
                                "as_of": AS_OF, "source": "ecb-exr", "sourced": True}
        assert detail["snapshot_replay"]["matches"] is True
        assert detail["snapshot_replay"]["base_matches"] is True


def test_foreign_currency_fee_rejected_when_rate_not_registered(settings):
    """没有登记汇率：拒绝并点名币种/时点，账本零分录、零费用行、零 proposal。"""
    with connection(settings) as conn:
        _approve(conn, settings, _usd_rule())
        with pytest.raises(ErpError) as ei:
            bill_event(conn, settings=settings, tenant_id=TENANT, event=_usd_event())
        assert ei.value.code == "FX_UNSOURCED"
        assert ei.value.detail["currency"] == "USD"
        assert ei.value.detail["as_of"] == AS_OF
        assert ei.value.detail["reason"] == "no_registered_rate"
        # 零写入（含 proposal：FX 预解析在任何写入之前）
        assert query_entries(conn, TENANT) == []
        assert list_proposals(conn, TENANT) == []
        import psycopg
        raw = psycopg.connect(settings.database_url, autocommit=True)
        try:
            assert raw.execute("SELECT count(*) FROM fee_details").fetchone()[0] == 0
        finally:
            raw.close()


def test_deleted_fx_fact_turns_previously_billed_fee_into_rejection(settings):
    """删掉汇率事实后重跑（新事件）：拒绝且零分录——出处纪律不因历史成功而放宽。"""
    with connection(settings) as conn:
        seed_fx_facts(conn, [{"quote_currency": "USD", "rate": "7.0", "as_of": AS_OF,
                              "source": "ecb-exr"}], settings=settings)
        _approve(conn, settings, _usd_rule())
        first = bill_event(conn, settings=settings, tenant_id=TENANT,
                           event=_usd_event(stable="cb-keep", qty=1))
        assert first["fees"][0]["base_amount"] == "14.000000"
        import psycopg
        raw = psycopg.connect(settings.database_url, autocommit=True)
        try:
            raw.execute("DELETE FROM fx_facts WHERE quote_currency='USD'")
        finally:
            raw.close()
        with pytest.raises(ErpError) as ei:
            bill_event(conn, settings=settings, tenant_id=TENANT,
                       event=_usd_event(stable="cb-after-delete", qty=1))
        assert ei.value.code == "FX_UNSOURCED"
        # 历史费用行原样保留，新事件零新增
        assert len(first["fees"]) == 1
        assert query_entries(conn, TENANT) == []


def test_rate_mismatch_with_fx_facts_rejected_at_ledger(settings):
    """「与登记不符」：分录声明的汇率必须在 fx_facts 有同值事实，否则 FX_UNSOURCED。"""
    with connection(settings) as conn:
        seed_fx_facts(conn, [{"quote_currency": "USD", "rate": "7.0", "as_of": AS_OF,
                              "source": "ecb-exr"}], settings=settings)
        with pytest.raises(ErpError) as ei:
            submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id="bad-fx",
                            lines=[
                                {"account_code": "1300", "direction": "debit", "amount": 10,
                                 "currency": "USD", "fx_rate": "7.1", "fx_as_of": AS_OF,
                                 "fx_source": "ecb-exr"},
                                {"account_code": "4000", "direction": "credit", "amount": 10,
                                 "currency": "USD", "fx_rate": "7.1", "fx_as_of": AS_OF,
                                 "fx_source": "ecb-exr"},
                            ])
        assert ei.value.code == "FX_UNSOURCED"
        assert ei.value.detail["declared_rate"] == "7.1"
        assert Decimal(ei.value.detail["registered_rate"]) == Decimal("7.0")
        assert list_proposals(conn, TENANT) == []


def test_no_fx_source_configured_is_rejected_not_defaulted(settings):
    """规则与默认源都没有出处 → 拒绝（不猜汇率）。"""
    no_source = Settings(database_url=settings.database_url, tenant_id=TENANT,
                         base_currency="CNY", fx_source="")
    with connection(settings) as conn:
        seed_fx_facts(conn, [{"quote_currency": "USD", "rate": "7.0", "as_of": AS_OF,
                              "source": "ecb-exr"}], settings=settings)
        _approve(conn, settings, _usd_rule())
        with pytest.raises(ErpError) as ei:
            bill_event(conn, settings=no_source, tenant_id=TENANT, event=_usd_event())
        assert ei.value.code == "FX_UNSOURCED"
        assert ei.value.detail["reason"] == "no_source"
        assert query_entries(conn, TENANT) == []


def test_rule_config_blank_fx_source_rejected(settings):
    with connection(settings) as conn:
        with pytest.raises(ErpError) as ei:
            submit_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                config=_usd_rule(fx_source="   "))
        assert ei.value.code == "FX_UNSOURCED"


def test_mixed_currency_batch_groups_lines_and_stays_balanced(settings):
    """同事件批 CNY + USD 两种费用：分录按币种分组，借贷仍平（amount_base 相等）。"""
    with connection(settings) as conn:
        seed_fx_facts(conn, [{"quote_currency": "USD", "rate": "7.0", "as_of": AS_OF,
                              "source": "ecb-exr"}], settings=settings)
        # 两个收费项命中同一事件类型：一个本位币、一个外币
        _approve(conn, settings, {
            "fee_item": "仓储费", "event_kind": "crossborder_order", "mode": "unit",
            "unit_price": 0.5, "measure": "quantity", "rate_source": "pricebook-2026Q4",
        })
        _approve(conn, settings, _usd_rule())
        res = bill_event(conn, settings=settings, tenant_id=TENANT, event=_usd_event(qty=10))
        assert len(res["fees"]) == 2
        by_item = {f["fee_item"]: f for f in res["fees"]}
        assert by_item["仓储费"]["base_amount"] == "5.000000"
        assert by_item["跨境服务费"]["base_amount"] == "140.000000"

        confirmed = confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                                     stable_id="cb-1:fee-journal")
        assert confirmed["status"] == "confirmed"
        entries = query_entries(conn, TENANT)
        currencies = {(e["currency"], e["account_code"], e["direction"]) for e in entries}
        assert ("CNY", "1300", "debit") in currencies
        assert ("CNY", "4000", "credit") in currencies
        assert ("USD", "1300", "debit") in currencies
        assert ("USD", "4000", "credit") in currencies
        bal = _balances(conn, settings)
        assert bal["1300"] == Decimal("145.000000")   # CNY 5 + USD 140
        assert bal["4000"] == Decimal("-145.000000")


def test_reversal_copies_fx_four_elements(settings):
    with connection(settings) as conn:
        seed_fx_facts(conn, [{"quote_currency": "USD", "rate": "7.0", "as_of": AS_OF,
                              "source": "ecb-exr"}], settings=settings)
        _approve(conn, settings, _usd_rule())
        res = bill_event(conn, settings=settings, tenant_id=TENANT, event=_usd_event(qty=10))
        fee_id = res["fees"][0]["fee_id"]
        rev = reverse_fee(conn, settings=settings, tenant_id=TENANT, fee_id=fee_id,
                          reason="计费口径调整")
        detail = get_fee_detail(conn, settings=settings, tenant_id=TENANT,
                                fee_id=rev["reversal_fee_id"])
        assert detail["fee"]["status"] == "reversed"
        assert detail["fee"]["original_amount"] == "20.000000"
        assert detail["fee"]["base_amount"] == "140.000000"
        assert detail["fee"]["fx_rate"] == "7.0000000000"
        assert detail["fee"]["fx_source"] == "ecb-exr"
