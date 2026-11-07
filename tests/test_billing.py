"""计费规则引擎（add-supply-chain-vertical 任务 1.x）。

覆盖验收：规则版本与审批（重放一致）/ 冲突拦截（重叠+空洞）/ 事件计费幂等重推 /
快照重算一致 / 红冲可溯（原行原样）/ 四层溯源 / 无出处费率拒绝 / 试算差异 /
机器车道白名单 / 跨租户拒绝。
全部对真实 Postgres 跑（append-only 触发器与 UNIQUE 幂等不可 mock）。
"""
from __future__ import annotations

import psycopg
import pytest

from fd_erp_ledger_mcp.billing import (approve_billing_rule, bill_event,
                                       bill_period_snapshot, compute_fee,
                                       get_fee_detail, list_billing_rules,
                                       list_fees, reverse_fee,
                                       simulate_billing, submit_billing_rule)
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.ledger import compute_balance, query_entries
from fd_erp_ledger_mcp.proposals import (confirm_proposal, list_proposals,
                                         submit_proposal)

TENANT = "acme"


def _rule_config(**over):
    cfg = {
        "fee_item": "仓储费",
        "event_kind": "storage_day",
        "mode": "unit",
        "unit_price": 0.5,
        "measure": "quantity",
        "rate_source": "pricebook-2026Q4",
    }
    cfg.update(over)
    return {k: v for k, v in cfg.items() if v is not None}


def _event(stable="ev-1", qty=100, **over):
    ev = {
        "stable_id": stable,
        "kind": "storage_day",
        "occurred_at": "2026-11-01",
        "measures": {"quantity": qty, "amount": qty * 10},
        "refs": {"document": "SO-001", "job": "WH-88"},
    }
    ev.update(over)
    return ev


def _submit_and_approve(conn, settings, cfg, rule_key=None, approver="human"):
    if rule_key:
        cfg = dict(cfg, rule_key=rule_key)
    r = submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=cfg)
    approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                         rule_id=r["rule_id"], version=r["version"], approved_by=approver)
    return r


# --------------------------- 规则版本与审批（重放一致） ---------------------------

def test_rule_versioning_and_approval_replay(settings):
    with connection(settings) as conn:
        r1 = submit_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                 config=_rule_config())
        assert r1["status"] == "pending_approval"
        r2 = submit_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                 config=_rule_config(unit_price=0.6))
        assert r2["version"] == r1["version"] + 1  # 同 rule_key 版本递增

        a1 = approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                  rule_id=r1["rule_id"], version=r1["version"])
        assert a1["status"] == "active" and a1["idempotent_hit"] is False
        # 重放同一审批 → 幂等返回首次结果（重放一致）
        a1b = approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                   rule_id=r1["rule_id"], version=r1["version"])
        assert a1b["status"] == "active" and a1b["idempotent_hit"] is True

        rules = list_billing_rules(conn, TENANT, status="active")
        assert [r["rule_id"] for r in rules] == [r1["rule_id"]]
        # 新版本上线 → 旧版本 succession 退役
        approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                             rule_id=r2["rule_id"], version=r2["version"])
        statuses = {r["rule_id"]: r["status"] for r in list_billing_rules(conn, TENANT)}
        assert statuses[r1["rule_id"]] == "retired"
        assert statuses[r2["rule_id"]] == "active"

        # 非法状态机：draft 不存在（submit 即 pending）；已 retired 不可再审批
        with pytest.raises(ErpError) as ei:
            approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                 rule_id=r1["rule_id"], version=r1["version"])
        assert ei.value.code == "RULE_STATE"


def test_program_self_approval_rejected(settings):
    with connection(settings) as conn:
        r = submit_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                config=_rule_config())
        with pytest.raises(ErpError) as ei:
            approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                 rule_id=r["rule_id"], version=r["version"],
                                 approved_by="billing-engine")
        assert ei.value.code == "INVALID_ARGUMENT"
        rules = list_billing_rules(conn, TENANT)
        assert rules[0]["status"] == "pending_approval"  # 仍待人工


# --------------------------- 冲突拦截 ---------------------------

def test_conflict_overlap_blocked_without_priority(settings):
    with connection(settings) as conn:
        a = submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=_rule_config(
            rule_key="storage-std", effective_from="2026-01-01", effective_to="2026-12-31",
            priority=10))
        approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                             rule_id=a["rule_id"], version=a["version"])
        b = submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=_rule_config(
            rule_key="storage-cold", effective_from="2026-04-01", effective_to="2026-09-30"))
        with pytest.raises(ErpError) as ei:
            approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                 rule_id=b["rule_id"], version=b["version"])
        assert ei.value.code == "RULE_CONFLICT"
        conf = ei.value.detail["conflicts"]
        assert conf and conf[0]["type"] == "overlap"
        # 未处置 MUST NOT active：被拦截规则保持 pending_approval
        rules = {r["rule_id"]: r["status"] for r in list_billing_rules(conn, TENANT)}
        assert rules[b["rule_id"]] == "pending_approval"

        # 处置：双方显式且不等的 priority（1 优先于 10）→ 重叠可上线
        b2 = submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=_rule_config(
            rule_key="storage-cold", effective_from="2026-04-01", effective_to="2026-09-30",
            priority=1, unit_price=0.8))
        approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                             rule_id=b2["rule_id"], version=b2["version"])
        # 同 fee_item 双 active（优先级区分），重叠窗口内事件计费取最高优先级
        res = bill_event(conn, settings=settings, tenant_id=TENANT,
                         event=_event(stable="ev-overlap", occurred_at="2026-06-15", qty=50))
        assert len(res["fees"]) == 1
        assert res["fees"][0]["rule_id"] == b2["rule_id"]  # priority=1 数值更小者优先
        assert res["fees"][0]["amount"] == "40.000000"  # 0.8 × 50


def test_conflict_coverage_gap_blocked(settings):
    with connection(settings) as conn:
        a = submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=_rule_config(
            rule_key="g-apr", effective_from="2026-01-01", effective_to="2026-01-10"))
        approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                             rule_id=a["rule_id"], version=a["version"])
        # 01-11 起紧邻衔接 → 无空洞，可上线
        ok = submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=_rule_config(
            rule_key="g-b-adjacent", effective_from="2026-01-11", effective_to="2026-01-20"))
        approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                             rule_id=ok["rule_id"], version=ok["version"])
        # 02-01 起 → 01-21..01-31 空洞 → 拦截
        c = submit_billing_rule(conn, settings=settings, tenant_id=TENANT, config=_rule_config(
            rule_key="g-c", effective_from="2026-02-01", effective_to="2026-02-28"))
        with pytest.raises(ErpError) as ei:
            approve_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                 rule_id=c["rule_id"], version=c["version"])
        assert ei.value.code == "RULE_CONFLICT"
        assert any(x["type"] == "gap" for x in ei.value.detail["conflicts"])


# --------------------------- 事件计费：幂等重推 ---------------------------

def test_bill_event_idempotent_repush(settings):
    with connection(settings) as conn:
        _submit_and_approve(conn, settings, _rule_config())
        res1 = bill_event(conn, settings=settings, tenant_id=TENANT, event=_event(qty=120))
        assert res1["duplicate"] is False
        assert len(res1["fees"]) == 1
        assert res1["fees"][0]["amount"] == "60.000000"  # 0.5 × 120
        # 同 stable_id 重推 → 返回首次结果，不重复计费
        res2 = bill_event(conn, settings=settings, tenant_id=TENANT, event=_event(qty=120))
        assert res2["duplicate"] is True
        assert [f["fee_id"] for f in res2["fees"]] == [f["fee_id"] for f in res1["fees"]]
        assert len(list_fees(conn, TENANT)) == 1
        # UNIQUE(tenant_id, stable_id) 是 DB 兜底：直插重复行被唯一索引拒绝
        with pytest.raises(psycopg.errors.Error):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO fee_details(tenant_id, rule_id, rule_version, stable_id, "
                        "event_stable_id, fee_item, snapshot, amount, currency) "
                        "SELECT tenant_id, rule_id, rule_version, stable_id, event_stable_id, "
                        "fee_item, snapshot, amount, currency FROM fee_details LIMIT 1")


def test_bill_event_no_matching_rule_is_honest_empty(settings):
    with connection(settings) as conn:
        _submit_and_approve(conn, settings, _rule_config())
        res = bill_event(conn, settings=settings, tenant_id=TENANT,
                         event=_event(stable="ev-other", kind="unknown_kind"))
        assert res["fees"] == [] and res["duplicate"] is False
        assert list_fees(conn, TENANT) == []


# --------------------------- 快照重算一致 ---------------------------

def test_snapshot_recompute_consistent_event_and_period(settings):
    with connection(settings) as conn:
        _submit_and_approve(conn, settings, _rule_config())
        res = bill_event(conn, settings=settings, tenant_id=TENANT, event=_event(qty=333))
        fid = res["fees"][0]["fee_id"]
        detail = get_fee_detail(conn, settings=settings, tenant_id=TENANT, fee_id=fid)
        assert detail["snapshot_replay"]["matches"] is True
        assert detail["snapshot_replay"]["amount"] == "166.500000"  # 0.5 × 333
        # 阶梯 + 封顶 + 最低消费的组合快照重算（独立 event_kind 避免同事件多规则命中）
        _submit_and_approve(conn, settings, _rule_config(
            rule_key="tiered", mode="tiered", fee_item="搬运费", event_kind="handling",
            tiers=[{"up_to": 100, "unit_price": 1}, {"up_to": None, "unit_price": 0.6}],
            cap_amount=200, minimum_amount=10))
        res2 = bill_event(conn, settings=settings, tenant_id=TENANT,
                          event=_event(stable="ev-tier", kind="handling", qty=250,
                                       measures={"quantity": 250}))
        # 100×1 + 150×0.6 = 190 → 封顶 200 不触发 → 190
        assert res2["fees"][0]["amount"] == "190.000000"
        d2 = get_fee_detail(conn, settings=settings, tenant_id=TENANT,
                            fee_id=res2["fees"][0]["fee_id"])
        assert d2["snapshot_replay"]["matches"] is True

        # 日终快照计费：存储费 = 日终库存快照 × 费率；幂等 + 重算一致
        snap = bill_period_snapshot(conn, settings=settings, tenant_id=TENANT,
                                    period="2026-10-31",
                                    snapshot={"fee_item": "仓储费", "as_of": "2026-10-31",
                                              "measures": {"quantity": 1000},
                                              "refs": {"document": "INV-SNAP-1031"}})
        assert snap["duplicate"] is False
        assert snap["fees"][0]["amount"] == "500.000000"  # 0.5 × 1000
        snap2 = bill_period_snapshot(conn, settings=settings, tenant_id=TENANT,
                                     period="2026-10-31",
                                     snapshot={"fee_item": "仓储费", "as_of": "2026-10-31",
                                               "measures": {"quantity": 9999}})
        assert snap2["duplicate"] is True  # 同期间重推返回首次结果
        assert snap2["fees"][0]["amount"] == "500.000000"
        d3 = get_fee_detail(conn, settings=settings, tenant_id=TENANT,
                            fee_id=snap["fees"][0]["fee_id"])
        assert d3["snapshot_replay"]["matches"] is True
        assert compute_fee(d3["snapshot"]["rule_config"], d3["snapshot"]["measures_used"]) \
            == 500


# --------------------------- 红冲可溯（原行原样） ---------------------------

def test_reverse_fee_traceable_original_untouched(settings):
    with connection(settings) as conn:
        _submit_and_approve(conn, settings, _rule_config())
        res = bill_event(conn, settings=settings, tenant_id=TENANT, event=_event(qty=100))
        fee_id = res["fees"][0]["fee_id"]
        prop_id = res["fees"][0]["journal_proposal_id"]
        assert prop_id is not None
        confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                         stable_id="ev-1:fee-journal")
        before = get_fee_detail(conn, settings=settings, tenant_id=TENANT, fee_id=fee_id)
        bal_before = {r["account_code"]: r["balance"]
                      for r in compute_balance(conn, TENANT, None, settings=settings)}

        rev = reverse_fee(conn, settings=settings, tenant_id=TENANT, fee_id=fee_id,
                          reason="多计一天")
        assert rev["idempotent_hit"] is False
        assert rev["journal_proposal_id"] is not None
        # 反向 proposal 走确认车道：落账前账本不变（按返回的 proposal_id 确认）
        confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                         proposal_id=rev["journal_proposal_id"])
        bal_after = {r["account_code"]: r["balance"]
                     for r in compute_balance(conn, TENANT, None, settings=settings)}
        # 50 借 1300 → 红冲后 1300 归零
        assert bal_before["1300"] == "50.000000"
        assert bal_after["1300"] == "0.000000"

        # 原行原样：逐字段比对红冲前后
        after = get_fee_detail(conn, settings=settings, tenant_id=TENANT, fee_id=fee_id)
        assert after["fee"]["amount"] == before["fee"]["amount"]
        assert after["fee"]["status"] == before["fee"]["status"] == "normal"
        assert after["fee"]["reversal_of"] is None
        assert after["snapshot"] == before["snapshot"]
        assert after["is_reversed"] is True  # 已被红冲是派生态（由反向行存在推导）
        assert len(after["reversals"]) == 1
        assert after["reversals"][0]["status"] == "reversed"
        # 红冲幂等：再冲同一费用返回首次结果
        rev2 = reverse_fee(conn, settings=settings, tenant_id=TENANT, fee_id=fee_id,
                           reason="重复红冲")
        assert rev2["idempotent_hit"] is True
        assert rev2["reversal_fee_id"] == rev["reversal_fee_id"]
        assert len(list_fees(conn, TENANT)) == 2  # 原行 + 一条反向行


def test_fee_details_append_only_enforced(settings):
    with connection(settings) as conn:
        _submit_and_approve(conn, settings, _rule_config())
        res = bill_event(conn, settings=settings, tenant_id=TENANT, event=_event())
        fee_id = res["fees"][0]["fee_id"]
        raw = psycopg.connect(settings.database_url, autocommit=True)
        try:
            with pytest.raises(psycopg.errors.Error) as ei:
                raw.execute("UPDATE fee_details SET amount=1 WHERE id=%s", (fee_id,))
            assert "ERP_APPEND_ONLY" in str(ei.value)
            with pytest.raises(psycopg.errors.Error) as ei2:
                raw.execute("DELETE FROM fee_details WHERE id=%s", (fee_id,))
            assert "ERP_APPEND_ONLY" in str(ei2.value)
            with pytest.raises(psycopg.errors.Error) as ei3:
                raw.execute("TRUNCATE fee_details")
            assert "ERP_APPEND_ONLY" in str(ei3.value)
        finally:
            raw.close()


# --------------------------- 四层溯源 ---------------------------

def test_four_layer_provenance_chain(settings):
    with connection(settings) as conn:
        _submit_and_approve(conn, settings, _rule_config())
        res = bill_event(conn, settings=settings, tenant_id=TENANT, event=_event())
        detail = get_fee_detail(conn, settings=settings, tenant_id=TENANT,
                                fee_id=res["fees"][0]["fee_id"])
        chain = detail["chain"]
        # 第 1 层：费用项
        assert chain["fee_item"] == "仓储费"
        # 第 2 层：业务单据
        assert chain["document"] == {"document": "SO-001", "job": "WH-88"}
        # 第 3 层：作业事件
        assert chain["job_event"]["stable_id"] == "ev-1"
        assert chain["job_event"]["kind"] == "storage_day"
        assert chain["job_event"]["measures"] == {"quantity": 100, "amount": 1000}
        # 第 4 层：计费规则版本（含费率出处）
        assert chain["rule"]["rate_source"] == "pricebook-2026Q4"
        assert chain["rule"]["version"] == 1
        assert detail["journal"]["proposal_stable_id"] == "ev-1:fee-journal"


# --------------------------- 无出处费率拒绝 ---------------------------

def test_unsourced_rate_rejected(settings):
    with connection(settings) as conn:
        for bad in ({"rate_source": None}, {"rate_source": ""}, {"rate_source": "   "}):
            with pytest.raises(ErpError) as ei:
                submit_billing_rule(conn, settings=settings, tenant_id=TENANT,
                                    config=_rule_config(**bad))
            assert ei.value.code == "RATE_UNSOURCED"
        assert list_billing_rules(conn, TENANT) == []
        # 试算同受出处纪律约束
        with pytest.raises(ErpError) as ei2:
            simulate_billing(conn, settings=settings, tenant_id=TENANT,
                             config=_rule_config(rate_source=None), events=[_event()])
        assert ei2.value.code == "RATE_UNSOURCED"


# --------------------------- 试算差异 ---------------------------

def test_simulate_billing_diff(settings):
    with connection(settings) as conn:
        _submit_and_approve(conn, settings, _rule_config(unit_price=1.0, fee_item="仓储费"))
        events = [_event(stable="h-1", qty=100), _event(stable="h-2", qty=200)]
        out = simulate_billing(conn, settings=settings, tenant_id=TENANT,
                               config=_rule_config(unit_price=0.8), events=events)
        ds = out["diff_summary"]
        assert ds["event_count"] == 2 and ds["compared"] == 2
        assert ds["old_total"] == "300.000000"  # 1.0 × (100+200)
        assert ds["new_total"] == "240.000000"  # 0.8 × (100+200)
        assert ds["delta"] == "-60.000000"
        assert ds["per_event"][0] == {"stable_id": "h-1", "occurred_at": "2026-11-01",
                                      "old_amount": "100.000000",
                                      "new_amount": "80.000000",
                                      "delta": "-20.000000",
                                      "old_rule": "仓储费@v1",
                                      "new_breakdown": {"mode": "unit",
                                                        "applied_limits": {}}}
        # kind 不匹配的事件如实跳过
        out2 = simulate_billing(conn, settings=settings, tenant_id=TENANT,
                                config=_rule_config(unit_price=0.8),
                                events=[_event(stable="h-3", kind="other")])
        assert out2["diff_summary"]["skipped_kind_mismatch"] == 1
        assert out2["diff_summary"]["new_total"] == "0.000000"


# --------------------------- 与账本衔接：机器车道 ---------------------------

def test_machine_lane_whitelist_and_no_direct_posting(settings):
    with connection(settings) as conn:
        _submit_and_approve(conn, settings, _rule_config())
        res = bill_event(conn, settings=settings, tenant_id=TENANT, event=_event(qty=40))
        prop_id = res["fees"][0]["journal_proposal_id"]
        assert prop_id is not None
        # 计费引擎 MUST NOT 绕过确认车道：bill 后 journal_entries 零变化
        assert query_entries(conn, TENANT) == []
        props = list_proposals(conn, TENANT)
        mine = [p for p in props if p["stable_id"] == "ev-1:fee-journal"]
        assert mine and mine[0]["status"] == "pending"
        # 确认门落账：借 1300 / 贷 4000
        confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                         stable_id="ev-1:fee-journal")
        entries = query_entries(conn, TENANT)
        assert {(e["account_code"], e["direction"]) for e in entries} == \
            {("1300", "debit"), ("4000", "credit")}
        # 机器车道白名单闸：billing-engine 名义提交白名单外科目 → 拒绝
        with pytest.raises(ErpError) as ei:
            submit_proposal(conn, settings=settings, tenant_id=TENANT,
                            stable_id="lane-evil", submitted_by="billing-engine",
                            lines=[{"account_code": "1000", "direction": "debit", "amount": 1},
                                   {"account_code": "4000", "direction": "credit", "amount": 1}])
        assert ei.value.code == "INVALID_ARGUMENT"
        assert "1000" in str(ei.value.detail["forbidden_accounts"])
        # 人工车道不受白名单限制
        p = submit_proposal(conn, settings=settings, tenant_id=TENANT,
                            stable_id="lane-human",
                            lines=[{"account_code": "1000", "direction": "debit", "amount": 1},
                                   {"account_code": "4000", "direction": "credit", "amount": 1}])
        assert p["status"] == "pending"


# --------------------------- 跨租户拒绝 ---------------------------

def test_cross_tenant_denied(settings):
    with connection(settings) as conn:
        _submit_and_approve(conn, settings, _rule_config())
        res = bill_event(conn, settings=settings, tenant_id=TENANT, event=_event())
        fee_id = res["fees"][0]["fee_id"]
        from fd_erp_ledger_mcp.tenancy import resolve_tenant
        with pytest.raises(ErpError) as ei:
            resolve_tenant(conn, settings, "other-corp")
        assert ei.value.code == "TENANT_DENIED"
        # 其他租户名下查不到该费用
        rows = list_fees(conn, "other-corp")
        assert all(f["fee_id"] != fee_id for f in rows)
