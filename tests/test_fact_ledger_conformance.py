"""事件层对 fact-ledger-kernel R1–R8 的逐条继承（change: establish-erp-event-kernel 1.3）。

八个用例组各对应一条纪律：删掉对应实现，该组必须变红（本文件不是形状检查，
是纪律检查）。全部对真实 Postgres 跑——DB 约束层不可 mock。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from fd_erp_ledger_mcp import events
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError

TENANT = "acme"


def _received(stable_id="grn-1", occurred_at=None, qty=120):
    return {
        "sku": "SKU-1",
        "quantity": qty,
        "unit": "pcs",
        "warehouse": "WH1",
        "lot": "L2301",
    }, occurred_at or datetime.now(timezone.utc)


def _append(conn, settings, stable_id="grn-1", lane="ingest", occurred_at=None):
    payload, occurred = _received(stable_id, occurred_at)
    return events.append_event(
        conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
        stable_id=stable_id, occurred_at=occurred, payload=payload, lane=lane,
    )


# ------------------------------- R1 事实只追加 -------------------------------

def test_r1_delete_is_denied(settings):
    with connection(settings) as conn:
        ev = _append(conn, settings)
        with pytest.raises(psycopg.Error) as ei:
            conn.execute("DELETE FROM business_events WHERE id=%s", (ev["id"],))
        assert "append-only" in str(ei.value)
        assert conn.execute("SELECT COUNT(*) FROM business_events").fetchone()[0] == 1


def test_r1_value_fields_are_immutable(settings):
    """payload / event_type / occurred_at / stable_id / lane 一律不可改。"""
    with connection(settings) as conn:
        ev = _append(conn, settings)
        with pytest.raises(psycopg.Error) as ei:
            conn.execute("UPDATE business_events SET payload='{\"sku\":\"X\"}'::jsonb WHERE id=%s",
                         (ev["id"],))
        assert "ERP_EVENT_IMMUTABLE" in str(ei.value)
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE business_events SET stable_id='other' WHERE id=%s", (ev["id"],))


def test_r1_truncate_is_denied(settings):
    """TRUNCATE 必须走 CASCADE 才到得了表（FK 先拦一遍），两道都必须拦住。

    单表 TRUNCATE 被外键挡、CASCADE 被 append-only 触发器挡——这条用例走后者，
    因为那才是真正的绕过路径。
    """
    with connection(settings) as conn:
        _append(conn, settings)
        with pytest.raises(psycopg.Error) as ei:
            conn.execute("TRUNCATE business_events CASCADE")
        assert "append-only" in str(ei.value)
        assert conn.execute("SELECT COUNT(*) FROM business_events").fetchone()[0] == 1


def test_r1_revision_before_finalization_keeps_old_value(settings):
    """定型前可修订，但旧值、修订来源、修订时点都必须留下。"""
    with connection(settings) as conn:
        _append(conn, settings, occurred_at=datetime.now(timezone.utc))
        out = events.revise_event(
            conn, tenant_id=TENANT, stable_id="grn-1", lane="ingest",
            payload={"sku": "SKU-1", "quantity": 118, "unit": "pcs",
                     "warehouse": "WH1", "lot": "L2301"},
            actor="ops", reason="到货复点",
        )
        assert out["payload"]["quantity"] == 118
        assert len(out["revisions"]) == 1
        assert out["revisions"][0]["old_payload"]["quantity"] == 120
        assert out["revisions"][0]["revised_by"] == "ops"
        assert out["revisions"][0]["reason"] == "到货复点"


def test_r1_revision_after_finalization_is_denied(settings):
    """日粒度事实在次日之前定型：昨天的事件已定型，改不动，只能冲正。"""
    past = datetime.now(timezone.utc) - timedelta(days=3)
    with connection(settings) as conn:
        _append(conn, settings, occurred_at=past)
        with pytest.raises(psycopg.Error) as ei:
            conn.execute("SELECT erp_revise_event(%s,%s,%s,%s::jsonb,%s,%s)",
                         (TENANT, "grn-1", "ingest",
                          '{"sku":"SKU-1","quantity":1,"unit":"pcs","warehouse":"WH1"}',
                          "ops", "事后改数"))
        assert "ERP_EVENT_IMMUTABLE" in str(ei.value)


def test_r1_finalization_is_period_derived_not_global_seconds(settings):
    """定型判定按事件自身周期：同一 now() 下，昨天定型、今天未定型。

    若改成"写入距今 N 秒"的全局阈值，本用例必红。
    """
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT finality_granularity, revision_window FROM event_types "
            "WHERE event_type='goods_received'").fetchone()
        gran, window = row
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today - timedelta(days=1)
        f_y = conn.execute("SELECT erp_event_is_finalized(%s,%s,%s)", (yesterday, gran, window)).fetchone()[0]
        f_t = conn.execute("SELECT erp_event_is_finalized(%s,%s,%s)", (today, gran, window)).fetchone()[0]
        assert f_y is True and f_t is False


# ------------------------------- R2 更正只冲正 -------------------------------

def test_r2_reversal_links_original_and_marks_it_reversed(settings):
    with connection(settings) as conn:
        _append(conn, settings)
        out = events.reverse_event(conn, tenant_id=TENANT, stable_id="grn-1", lane="ingest",
                                   reversal_stable_id="grn-1-rev", actor="ops", note="批次错")
        assert out["reversal"]["reversal_of"] == "grn-1"
        assert out["original"]["status"] == "reversed"
        # 冲正不修改原事件内容
        assert out["original"]["payload"]["quantity"] == 120


def test_r2_illegal_status_transition_is_denied(settings):
    """pending 不能直接跳 reversed：状态迁移是封闭集。"""
    with connection(settings) as conn:
        ev = _append(conn, settings, lane="draft")
        assert ev["status"] == "pending"
        with pytest.raises(psycopg.Error) as ei:
            conn.execute("UPDATE business_events SET status='reversed' WHERE id=%s", (ev["id"],))
        assert "illegal status transition" in str(ei.value)


# ---------------------- R3 派生可重算且快照带水位 ----------------------

def test_r3_balance_is_recomputable_and_watermarked(settings):
    with connection(settings) as conn:
        ev = _append(conn, settings)
        bal = events.project_object_event(
            conn, tenant_id=TENANT, event_id=ev["id"], object_type="sku",
            object_key="SKU-1", quantity_delta=120, unit="pcs",
            dimension={"warehouse": "WH1", "lot": "L2301"})
        assert bal["quantity"] == 120
        assert bal["as_of_event_id"] == ev["id"]  # 水位
        assert bal["drift"] is False
        # 重算口径独立于读路径
        rec = conn.execute("SELECT * FROM erp_recompute_object_balance(%s,%s,%s,%s::jsonb)",
                           (TENANT, "sku", "SKU-1", '{"warehouse":"WH1","lot":"L2301"}')).fetchone()
        assert rec[0] == 120 and rec[2] == ev["id"]


def test_r3_drift_is_detected_not_silently_trusted(settings):
    """物化值被改错时，读取须报 drifted 并给出重算口径（以事件为准）。"""
    with connection(settings) as conn:
        ev = _append(conn, settings)
        events.project_object_event(conn, tenant_id=TENANT, event_id=ev["id"],
                                    object_type="sku", object_key="SKU-1",
                                    quantity_delta=120, unit="pcs", dimension={"warehouse": "WH1"})
        conn.execute("SELECT set_config('erp.object_projection','on',false)")
        conn.execute("UPDATE object_balances SET quantity=999 WHERE object_key='SKU-1'")
        bal = events.query_object_balance(conn, tenant_id=TENANT, object_type="sku",
                                          object_key="SKU-1", dimension={"warehouse": "WH1"})
        assert bal["drift"] is True
        assert bal["completeness"]["state"] == "drifted"
        assert bal["recomputed"]["quantity"] == 120


def test_r3_balance_is_not_directly_writable(settings):
    with connection(settings) as conn:
        ev = _append(conn, settings)
        events.project_object_event(conn, tenant_id=TENANT, event_id=ev["id"],
                                    object_type="sku", object_key="SKU-1",
                                    quantity_delta=120, unit="pcs")
        with pytest.raises(psycopg.Error) as ei:
            conn.execute("UPDATE object_balances SET quantity=5 WHERE object_key='SKU-1'")
        assert "projection-only" in str(ei.value)


# ---------------------- R4 写入以作用域+幂等键去重 ----------------------

def test_r4_same_stable_id_same_lane_is_deduped(settings):
    with connection(settings) as conn:
        first = _append(conn, settings)
        again = _append(conn, settings)
        assert first["id"] == again["id"]
        assert again.get("duplicate") is True
        assert conn.execute("SELECT COUNT(*) FROM business_events").fetchone()[0] == 1


def test_r4_two_lanes_have_independent_key_spaces(settings):
    with connection(settings) as conn:
        ing = _append(conn, settings, stable_id="grn-1", lane="ingest")
        drf = _append(conn, settings, stable_id="grn-1", lane="draft")
        assert ing["id"] != drf["id"]
        assert ing["status"] == "accepted"
        assert drf["status"] == "pending"


# ---------------------- R5 未过确认门的不算事实 ----------------------

def test_r5_draft_pending_is_not_a_fact(settings):
    """拟稿未确认：不进投影待办、不参与任何派生。确认后才算事实。"""
    with connection(settings) as conn:
        _append(conn, settings, stable_id="adj-1", lane="draft")
        assert events.pending_projection_events(conn, tenant_id=TENANT)["items"] == []

        events.decide_event(conn, tenant_id=TENANT, stable_id="adj-1", lane="draft",
                            decision="accepted", actor="ops")
        pending = events.pending_projection_events(conn, tenant_id=TENANT)
        assert [e["stable_id"] for e in pending["items"]] == ["adj-1"]


def test_r5_rejected_event_never_becomes_a_fact(settings):
    with connection(settings) as conn:
        _append(conn, settings, stable_id="adj-2", lane="draft")
        out = events.decide_event(conn, tenant_id=TENANT, stable_id="adj-2", lane="draft",
                                  decision="rejected", actor="ops", note="无依据")
        assert out["status"] == "rejected"
        assert events.pending_projection_events(conn, tenant_id=TENANT)["items"] == []


def test_r5_ingest_lane_needs_no_confirmation(settings):
    with connection(settings) as conn:
        ev = _append(conn, settings, lane="ingest")
        assert ev["status"] == "accepted"
        with pytest.raises(ErpError) as ei:
            events.decide_event(conn, tenant_id=TENANT, stable_id="grn-1", lane="ingest",
                                decision="accepted")
        assert ei.value.code == "EVENT_STATE"


# ---------------------- R6 状态封闭且迁移可判非法 ----------------------

def test_r6_status_is_a_closed_set(settings):
    with connection(settings) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO business_events(tenant_id,event_type,stable_id,occurred_at,"
                "payload,lane,status) VALUES (%s,'goods_received','x',now(),'{}'::jsonb,"
                "'ingest','weird')", (TENANT,))
        with pytest.raises(ErpError) as ei:
            events.decide_event(conn, tenant_id=TENANT, stable_id="nope", lane="draft",
                                decision="accepted")
        assert ei.value.code == "NOT_FOUND"


# ---------------------- R7 续跑只从已提交处继续 ----------------------

def test_r7_projection_resumes_from_watermark_without_redoing(settings):
    with connection(settings) as conn:
        a = _append(conn, settings, stable_id="grn-a")
        b = _append(conn, settings, stable_id="grn-b")

        conn.execute("SELECT erp_project_object_event(%s,%s,%s,%s::jsonb,%s,%s)",
                     (a["id"], "sku", "SKU-A", "{}", 10, "pcs"))
        # 从水位 a 之后续跑：只看到 b
        rest = events.pending_projection_events(conn, tenant_id=TENANT, after_event_id=a["id"])
        assert [e["stable_id"] for e in rest["items"]] == ["grn-b"]

        conn.execute("SELECT erp_project_object_event(%s,%s,%s,%s::jsonb,%s,%s)",
                     (b["id"], "sku", "SKU-A", "{}", 5, "pcs"))
        # 重跑不重做：同事件再投影一次，余额不变、增量行不增
        conn.execute("SELECT erp_project_object_event(%s,%s,%s,%s::jsonb,%s,%s)",
                     (b["id"], "sku", "SKU-A", "{}", 5, "pcs"))
        bal = events.query_object_balance(conn, tenant_id=TENANT, object_type="sku",
                                          object_key="SKU-A")
        assert bal["quantity"] == 15
        assert conn.execute("SELECT COUNT(*) FROM object_events").fetchone()[0] == 2


# ---------------------- R8 读取必须声明完整性 ----------------------

def test_r8_query_declares_completeness_and_truncation(settings):
    with connection(settings) as conn:
        for i in range(3):
            _append(conn, settings, stable_id=f"grn-{i}")
        out = events.query_events(conn, tenant_id=TENANT, limit=2)
        c = out["completeness"]
        assert len(out["items"]) == 2
        assert c["declared_total"] == 3
        assert c["returned"] == 2
        assert c["truncated"] is True
        assert c["state"] == "partial"

        full = events.query_events(conn, tenant_id=TENANT, limit=10)
        assert full["completeness"]["state"] == "complete"
        assert full["completeness"]["watermark"] >= 1


def test_r8_absent_object_declares_absent_state(settings):
    with connection(settings) as conn:
        bal = events.query_object_balance(conn, tenant_id=TENANT, object_type="sku",
                                          object_key="NO-SUCH")
        assert bal["quantity"] is None
        assert bal["completeness"]["state"] == "absent"
        assert bal["completeness"]["declared_total"] == 0


# ---------------------- unknown 与空值必须可区分 ----------------------

def test_unknown_is_first_class_and_empty_string_is_rejected(settings):
    with connection(settings) as conn:
        payload, occurred = _received("grn-u")
        payload["lot"] = events.UNKNOWN
        ev = events.append_event(conn, settings=settings, tenant_id=TENANT,
                                 event_type="goods_received", stable_id="grn-u",
                                 occurred_at=occurred, payload=payload)
        assert ev["payload"]["lot"] == events.UNKNOWN

        empty = dict(payload, warehouse="")
        with pytest.raises(ErpError) as ei:
            events.append_event(conn, settings=settings, tenant_id=TENANT,
                                event_type="goods_received", stable_id="grn-e",
                                occurred_at=occurred, payload=empty)
        assert ei.value.code == "INVALID_ARGUMENT"
        assert "unknown_sentinel" in (ei.value.detail or {})


def test_unregistered_event_type_is_rejected(settings):
    with connection(settings) as conn:
        with pytest.raises(ErpError) as ei:
            events.append_event(conn, settings=settings, tenant_id=TENANT,
                                event_type="no_such_event", stable_id="x-1",
                                occurred_at=datetime.now(timezone.utc), payload={})
        assert ei.value.code == "EVENT_UNKNOWN_TYPE"
