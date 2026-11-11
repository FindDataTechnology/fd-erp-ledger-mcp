"""数量面：对象投影、维度、单位冲突、与账本的边界（change 2.2/2.3 与 ledger-kernel 的
「数量类事实不属于账本」）。

关键不变量：数量只活在对象投影里，金额只活在分录里，两边不互相冒充。
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from fd_erp_ledger_mcp import events
from fd_erp_ledger_mcp.db import connection

TENANT = "acme"


def _receive(conn, settings, stable_id="grn-1", qty=120, lot="L2301", warehouse="WH1",
             unit="pcs"):
    return events.append_event(
        conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
        stable_id=stable_id, occurred_at=datetime.now(timezone.utc),
        payload={"sku": "SKU-1", "quantity": qty, "unit": unit,
                 "warehouse": warehouse, "lot": lot},
    )


def test_quantity_dimensions_are_read_back_field_by_field(settings):
    """单位/批次/序列号/仓库都必须能逐字段读回，不被折叠成金额。"""
    with connection(settings) as conn:
        ev = _receive(conn, settings)
        assert ev["payload"]["quantity"] == 120
        assert ev["payload"]["unit"] == "pcs"
        assert ev["payload"]["lot"] == "L2301"
        assert ev["payload"]["warehouse"] == "WH1"
        assert ev["status"] == "accepted"

        bal = events.project_object_event(
            conn, tenant_id=TENANT, event_id=ev["id"], object_type="sku",
            object_key="SKU-1", quantity_delta=120, unit="pcs",
            dimension={"warehouse": "WH1", "lot": "L2301"})
        assert bal["quantity"] == 120
        assert bal["unit"] == "pcs"
        assert bal["dimension"] == {"warehouse": "WH1", "lot": "L2301"}


def test_dimensions_are_independent_balances(settings):
    """同 SKU 不同仓库/批次是不同余额，不得混算。"""
    with connection(settings) as conn:
        a = _receive(conn, settings, stable_id="grn-a", warehouse="WH1", lot="L1")
        b = _receive(conn, settings, stable_id="grn-b", warehouse="WH2", lot="L2")
        events.project_object_event(conn, tenant_id=TENANT, event_id=a["id"],
                                    object_type="sku", object_key="SKU-1",
                                    quantity_delta=120, unit="pcs",
                                    dimension={"warehouse": "WH1", "lot": "L1"})
        events.project_object_event(conn, tenant_id=TENANT, event_id=b["id"],
                                    object_type="sku", object_key="SKU-1",
                                    quantity_delta=30, unit="pcs",
                                    dimension={"warehouse": "WH2", "lot": "L2"})
        w1 = events.query_object_balance(conn, tenant_id=TENANT, object_type="sku",
                                        object_key="SKU-1",
                                        dimension={"warehouse": "WH1", "lot": "L1"})
        w2 = events.query_object_balance(conn, tenant_id=TENANT, object_type="sku",
                                        object_key="SKU-1",
                                        dimension={"warehouse": "WH2", "lot": "L2"})
        assert (w1["quantity"], w2["quantity"]) == (120, 30)


def test_mixed_units_in_same_dimension_are_rejected(settings):
    """kg 与 pcs 相加是无意义的数——宁可拒写，也不静默算错。"""
    with connection(settings) as conn:
        a = _receive(conn, settings, stable_id="grn-kg", unit="kg")
        b = _receive(conn, settings, stable_id="grn-pcs", unit="pcs")
        events.project_object_event(conn, tenant_id=TENANT, event_id=a["id"],
                                    object_type="sku", object_key="SKU-1",
                                    quantity_delta=10, unit="kg",
                                    dimension={"warehouse": "WH1"})
        with pytest.raises(psycopg.Error) as ei:
            events.project_object_event(conn, tenant_id=TENANT, event_id=b["id"],
                                        object_type="sku", object_key="SKU-1",
                                        quantity_delta=10, unit="pcs",
                                        dimension={"warehouse": "WH1"})
        assert "mixes units" in str(ei.value)
        bal = events.query_object_balance(conn, tenant_id=TENANT, object_type="sku",
                                        object_key="SKU-1", dimension={"warehouse": "WH1"})
        assert bal["quantity"] == 10  # 被拒的那笔未生效


def test_object_identity_is_registered_on_first_projection(settings):
    with connection(settings) as conn:
        ev = _receive(conn, settings)
        events.project_object_event(conn, tenant_id=TENANT, event_id=ev["id"],
                                    object_type="sku", object_key="SKU-1",
                                    quantity_delta=1, unit="pcs")
        row = conn.execute("SELECT object_type, object_key FROM objects").fetchone()
        assert row == ("sku", "SKU-1")


def test_ledger_has_no_quantity_column(settings):
    """账本不是数量面：journal_entries 不得出现数量列。

    这条用例保证「数量走对象、金额走账本」不被后来的加列悄悄破坏。
    """
    with connection(settings) as conn:
        cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='journal_entries'").fetchall()]
    assert "amount" in cols
    for forbidden in ("quantity", "qty", "unit", "lot", "serial"):
        assert forbidden not in cols, f"账本出现数量列 {forbidden!r}：数量面不属于账本"


def test_projection_of_unknown_event_is_rejected(settings):
    """服务层给封闭码 NOT_FOUND；数据库函数自身也拒（两道都在）。"""
    from fd_erp_ledger_mcp.errors import ErpError
    with connection(settings) as conn:
        with pytest.raises(ErpError) as ei:
            events.project_object_event(conn, tenant_id=TENANT, event_id=999999,
                                        object_type="sku", object_key="SKU-1",
                                        quantity_delta=1, unit="pcs")
        assert ei.value.code == "NOT_FOUND"
        # 绕过服务层直调数据库函数同样被拒
        with pytest.raises(psycopg.Error) as di:
            conn.execute("SELECT erp_project_object_event(%s,%s,%s,%s::jsonb,%s,%s)",
                         (999999, "sku", "SKU-1", "{}", 1, "pcs"))
        assert "no event id" in str(di.value)


def test_pending_draft_event_cannot_be_projected(settings):
    """未过确认门的拟稿事件不算事实：投影必须拒，否则草稿能变成库存。"""
    with connection(settings) as conn:
        ev = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
            stable_id="grn-draft", occurred_at=datetime.now(timezone.utc), lane="draft",
            payload={"sku": "SKU-1", "quantity": 999, "unit": "pcs", "warehouse": "WH1"})
        from fd_erp_ledger_mcp.errors import ErpError
        with pytest.raises(ErpError) as ei:
            events.project_object_event(conn, tenant_id=TENANT, event_id=ev["id"],
                                        object_type="sku", object_key="SKU-1",
                                        quantity_delta=999, unit="pcs")
        assert ei.value.code == "EVENT_STATE"
        bal = events.query_object_balance(conn, tenant_id=TENANT, object_type="sku",
                                          object_key="SKU-1")
        assert bal["quantity"] is None
