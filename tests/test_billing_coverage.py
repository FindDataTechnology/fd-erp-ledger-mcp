"""收费项覆盖缺失阻断（optimize-erp-solution 5.5 / design D9）。

口径：「登记在用集合 − active 规则集合」非空即阻断，三个拦截点：规则审批上线、
事件计费、日终快照计费。登记/停用是运维目录动作（CLI register-fee-item），
未启用登记（租户零登记行）的存量租户不受影响（迁移过渡口径）。

真 Postgres：差集与状态机都在库里，mock 不出行为。
"""
from __future__ import annotations

import psycopg
import pytest

from fd_erp_ledger_mcp.billing import (approve_billing_rule, bill_event,
                                       bill_period_snapshot, register_fee_item,
                                       submit_billing_rule)
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.proposals import list_proposals

TENANT = "acme"


def _rule(fee_item, event_kind, **over):
    cfg = {"fee_item": fee_item, "event_kind": event_kind, "mode": "unit",
           "unit_price": 1, "measure": "quantity", "rate_source": "pricebook-2026Q4"}
    cfg.update(over)
    return {k: v for k, v in cfg.items() if v is not None}


def _submit(conn, settings, cfg):
    return submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=cfg)


def _approve(conn, settings, rule):
    return approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                rule_id=rule["rule_id"], version=rule["version"])


def _raw(settings):
    return psycopg.connect(settings.database_url, autocommit=True)


def _fee_count(settings) -> int:
    raw = _raw(settings)
    try:
        return int(raw.execute("SELECT count(*) FROM fee_details").fetchone()[0])
    finally:
        raw.close()


def test_approval_blocked_after_retiring_rule_without_replacement(settings):
    """停旧未上新：旧规则被停用、新规则还没上线时，审批被拒并点名该收费项。"""
    with connection(settings) as conn:
        register_fee_item(conn, tenant_id=TENANT, fee_item="仓储费",
                          event_kind="storage_day", source_ref="pricebook-2026Q4")
        storage = _submit(conn, settings, _rule("仓储费", "storage_day"))
        _approve(conn, settings, storage)  # post-state 覆盖 仓储费 → 放行

        # 运维停用旧规则（模拟"停旧"，不挂新规则）
        raw = _raw(settings)
        try:
            raw.execute("UPDATE billing_rules SET status='retired' "
                        "WHERE tenant_id=%s AND rule_key='仓储费'", (TENANT,))
        finally:
            raw.close()

        # 另一个收费项待上线：此时"仓储费"登记在用但无 active 规则 → 审批被拒并点名
        register_fee_item(conn, tenant_id=TENANT, fee_item="搬运费",
                          event_kind="handling", source_ref="pricebook-2026Q4")
        handling = _submit(conn, settings, _rule("搬运费", "handling"))
        with pytest.raises(ErpError) as ei:
            _approve(conn, settings, handling)
        assert ei.value.code == "FEE_COVERAGE_MISSING"
        assert ei.value.detail["stage"] == "rule_approval"
        assert ei.value.detail["fee_items"] == ["仓储费"]
        # 未处置 MUST NOT active
        raw = _raw(settings)
        try:
            status = raw.execute(
                "SELECT status FROM billing_rules WHERE id=%s", (handling["rule_id"],)
            ).fetchone()[0]
        finally:
            raw.close()
        assert status == "pending_approval"

        # 补上仓储费新规则 → 覆盖恢复，两个收费项都能上线
        storage_v2 = _submit(conn, settings, _rule("仓储费", "storage_day", unit_price=1.2))
        _approve(conn, settings, storage_v2)
        _approve(conn, settings, handling)


def test_event_billing_blocked_when_registered_item_lacks_active_rule(settings):
    """事件命中登记在用、但当前无 active 规则的收费项 → 拒绝并点名，不生成费用明细。"""
    with connection(settings) as conn:
        register_fee_item(conn, tenant_id=TENANT, fee_item="仓储费",
                          event_kind="storage_day")
        storage = _submit(conn, settings, _rule("仓储费", "storage_day"))
        _approve(conn, settings, storage)

        raw = _raw(settings)
        try:
            raw.execute("UPDATE billing_rules SET status='retired' "
                        "WHERE tenant_id=%s AND rule_key='仓储费'", (TENANT,))
        finally:
            raw.close()

        with pytest.raises(ErpError) as ei:
            bill_event(conn, settings=settings, tenant_id=TENANT, event={
                "stable_id": "ev-gap", "kind": "storage_day",
                "occurred_at": "2026-11-09", "measures": {"quantity": 10}})
        assert ei.value.code == "FEE_COVERAGE_MISSING"
        assert ei.value.detail["stage"] == "event_billing"
        assert ei.value.detail["fee_items"] == ["仓储费"]
        assert _fee_count(settings) == 0
        assert list_proposals(conn, TENANT) == []


def test_event_billing_blocked_when_matched_item_unregistered(settings):
    """命中未登记收费项同样拒绝（登记缺失不能靠"有规则"放行）。"""
    with connection(settings) as conn:
        register_fee_item(conn, tenant_id=TENANT, fee_item="仓储费",
                          event_kind="storage_day")
        _approve(conn, settings, _submit(conn, settings, _rule("仓储费", "storage_day")))
        # 另一收费项有 active 规则（同事件类型）但从未登记
        _approve(conn, settings, _submit(conn, settings,
                                        _rule("搬仓费", "storage_day", unit_price=0.2)))

        with pytest.raises(ErpError) as ei:
            bill_event(conn, settings=settings, tenant_id=TENANT, event={
                "stable_id": "ev-unreg", "kind": "storage_day",
                "occurred_at": "2026-11-09", "measures": {"quantity": 10}})
        assert ei.value.code == "FEE_COVERAGE_MISSING"
        assert ei.value.detail["unregistered_fee_items"] == ["搬仓费"]
        assert _fee_count(settings) == 0


def test_period_billing_blocked_on_coverage_gap_without_partial_batch(settings):
    """期间计费：任一登记在用项缺 active 规则 → 整批拒绝，不生成部分批次。"""
    with connection(settings) as conn:
        register_fee_item(conn, tenant_id=TENANT, fee_item="仓储费",
                          event_kind="storage_day")
        _approve(conn, settings, _submit(conn, settings, _rule("仓储费", "storage_day")))
        # 登记第二个收费项，但不给规则 → 覆盖缺失
        register_fee_item(conn, tenant_id=TENANT, fee_item="搬运费",
                          event_kind="handling")

        with pytest.raises(ErpError) as ei:
            bill_period_snapshot(
                conn, settings=settings, tenant_id=TENANT, period="2026-11-09",
                snapshot={"fee_item": "仓储费", "as_of": "2026-11-09",
                          "measures": {"quantity": 100}})
        assert ei.value.code == "FEE_COVERAGE_MISSING"
        assert ei.value.detail["stage"] == "period_billing"
        assert ei.value.detail["fee_items"] == ["搬运费"]
        assert _fee_count(settings) == 0
        assert list_proposals(conn, TENANT) == []


def test_period_billing_rejects_unregistered_requested_item(settings):
    with connection(settings) as conn:
        register_fee_item(conn, tenant_id=TENANT, fee_item="仓储费",
                          event_kind="storage_day")
        _approve(conn, settings, _submit(conn, settings, _rule("仓储费", "storage_day")))
        # 打包计费请求里出现未登记收费项（即使它也有 active 规则）
        _approve(conn, settings, _submit(conn, settings, _rule("搬运费", "handling")))
        with pytest.raises(ErpError) as ei:
            bill_period_snapshot(
                conn, settings=settings, tenant_id=TENANT, period="2026-11-09",
                snapshot={"fee_item": "搬运费", "as_of": "2026-11-09",
                          "measures": {"quantity": 5}})
        assert ei.value.code == "FEE_COVERAGE_MISSING"
        assert ei.value.detail["unregistered_fee_items"] == ["搬运费"]
        assert _fee_count(settings) == 0


def test_registry_transition_no_rows_means_no_gate(settings):
    """迁移过渡：租户没有任何登记行时不启用覆盖闸（存量行为不变）。"""
    with connection(settings) as conn:
        _approve(conn, settings, _submit(conn, settings, _rule("仓储费", "storage_day")))
        res = bill_event(conn, settings=settings, tenant_id=TENANT, event={
            "stable_id": "ev-legacy", "kind": "storage_day",
            "occurred_at": "2026-11-09", "measures": {"quantity": 10}})
        assert len(res["fees"]) == 1 and res["fees"][0]["amount"] == "10.000000"
        # retried/退役行也算"已启用登记"，差集立刻生效
        register_fee_item(conn, tenant_id=TENANT, fee_item="搬运费",
                          event_kind="handling", status="retired")
        with pytest.raises(ErpError) as ei:
            bill_period_snapshot(
                conn, settings=settings, tenant_id=TENANT, period="2026-11-10",
                snapshot={"fee_item": "搬运费", "as_of": "2026-11-10",
                          "measures": {"quantity": 1}})
        assert ei.value.code == "FEE_COVERAGE_MISSING"


def test_register_fee_item_idempotent_and_retire(settings):
    with connection(settings) as conn:
        first = register_fee_item(conn, tenant_id=TENANT, fee_item="仓储费",
                                  event_kind="storage_day")
        again = register_fee_item(conn, tenant_id=TENANT, fee_item="仓储费",
                                  event_kind="storage_day", source_ref="pricebook-2")
        assert first["status"] == again["status"] == "in_use"
        retired = register_fee_item(conn, tenant_id=TENANT, fee_item="仓储费",
                                    event_kind="storage_day", status="retired")
        assert retired["status"] == "retired"
        from fd_erp_ledger_mcp.billing import list_fee_items
        rows = list_fee_items(conn, TENANT)
        assert len(rows) == 1 and rows[0]["status"] == "retired"
        # 参数校验
        with pytest.raises(ErpError) as ei:
            register_fee_item(conn, tenant_id=TENANT, fee_item="  ", event_kind="x")
        assert ei.value.code == "INVALID_ARGUMENT"
        with pytest.raises(ErpError) as ei2:
            register_fee_item(conn, tenant_id=TENANT, fee_item="x", event_kind="y",
                              status="bogus")
        assert ei2.value.code == "INVALID_ARGUMENT"
