"""运行指标与结构化日志（optimize-erp-solution 6.1 / erp-operations-hardening「运行指标与结构化日志」）。

锁三件事：
  1. `/metrics` 真的挂得上、抓得到，且零配置（不连库）可抓；
  2. 四类运行信号都在指标里可读：proposal 提交/确认/拒绝、计费笔数与失败原因、
     对账任务状态与差异、工具时延与错误码；
  3. 脱敏：日志与指标不含凭据 / 连接串 / 租户明文（租户只以 12 位 sha256 前缀出现），
     且计费失败原因按封闭错误码分别成序列、不合并成单一错误计数。

指标名直接对齐 `fd_erp_ledger_mcp/observability.py` 的注册名（不另造名字）。
计数是进程级累加，故断言一律用「调用前后差值」，不写死绝对值。
"""
from __future__ import annotations

import asyncio
import json
import logging

import psycopg
import pytest
from fastmcp import Client
from prometheus_client.parser import text_string_to_metric_families

from fd_erp_ledger_mcp import observability as obs
from fd_erp_ledger_mcp.billing import (approve_billing_rule, bill_event,
                                       bill_period_snapshots, register_fee_item,
                                       submit_billing_rule)
from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.proposals import (confirm_proposal, reject_proposal,
                                         submit_proposal)
from fd_erp_ledger_mcp.recon.engine import enqueue, resume_orphans, wait_for_task
from fd_erp_ledger_mcp.server import create_server

#: 故意取一个长得不像业务名词的租户名，便于断言「明文不入日志/指标」。
TENANT = "tenant-observability-probe"


# ---------------------------------------------------------------------------
# 指标解析（prometheus 文本格式 → {(样本名, 标签元组): 值}）
# ---------------------------------------------------------------------------

def _samples() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    text = obs.render_metrics().decode()
    out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            out[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return out


def _value(samples, name: str, **labels: str) -> float:
    return samples.get((name, tuple(sorted(labels.items()))), 0.0)


def _delta(before, after, name: str, **labels: str) -> float:
    return _value(after, name, **labels) - _value(before, name, **labels)


def _route(app, path: str):
    for route in app.routes:
        if getattr(route, "path", "") == path:
            return route
    raise AssertionError(f"HTTP app 上找不到路由 {path}")


class _JsonCollector(logging.Handler):
    """把结构化日志行解析成 dict 收进列表（logger propagate=False，caplog 抓不到）。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(json.loads(record.getMessage()))
        except (TypeError, ValueError):
            self.records.append({"raw": record.getMessage()})


def _rule_cfg(fee_item: str, event_kind: str, *, unit_price=1, **over) -> dict:
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


def _insert_orphan(settings, task_id: str, *, status: str, request: dict | None) -> None:
    raw = psycopg.connect(settings.database_url, autocommit=True)
    try:
        raw.execute(
            "INSERT INTO recon_reports(task_id, tenant_id, status, stage, total, request) "
            "VALUES (%s,%s,%s,%s,0,%s::jsonb)",
            (task_id, TENANT, status, status,
             None if request is None else json.dumps(request, ensure_ascii=False)),
        )
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# `/metrics` 暴露面
# ---------------------------------------------------------------------------

@pytest.mark.no_db
def test_metrics_route_scrapable_without_touching_db():
    """零配置 server（database_url=None）也能抓 /metrics；抓不到即等于没有指标面。"""
    obs.PROPOSALS_SUBMITTED.labels(kind="journal").inc()  # 保证至少有一个样本可抓
    server = create_server(Settings(database_url=None))
    app = server.http_app()
    response = asyncio.run(_route(app, "/metrics").endpoint(None))
    assert response.headers["content-type"].startswith("text/plain")
    text = response.body.decode()
    assert "erp_proposals_submitted_total" in text
    # 纯内存指标，不含任何连接串 / 租户身份。
    assert "postgresql://" not in text
    assert "tenant_id=" not in text


# ---------------------------------------------------------------------------
# 四类运行信号
# ---------------------------------------------------------------------------

def test_metrics_count_proposals_and_tool_latency_and_error_codes(settings):
    before = _samples()
    with connection(settings) as conn:
        for stable in ("obs-1", "obs-2"):
            submit_proposal(conn, settings=settings, tenant_id=TENANT, stable_id=stable,
                            lines=[{"account_code": "1300", "direction": "debit",
                                    "amount": 10},
                                   {"account_code": "4000", "direction": "credit",
                                    "amount": 10}])
        assert confirm_proposal(conn, settings=settings, tenant_id=TENANT,
                                stable_id="obs-1")["status"] == "confirmed"
        assert reject_proposal(conn, tenant_id=TENANT,
                               stable_id="obs-2", reason="口径不对")["status"] == "rejected"

    server = create_server(settings)

    async def go():
        async with Client(server) as client:
            ok = await client.call_tool("query_balance", {})
            err = await client.call_tool("get_fee_detail", {"fee_id": 999999})
            return ok, err

    ok, err = asyncio.run(go())
    ok_data = ok.data if hasattr(ok, "data") else ok
    err_data = err.data if hasattr(err, "data") else err
    assert ok_data["ok"] is True
    assert err_data["ok"] is False
    assert err_data["error"]["code"] == "NOT_FOUND"

    after = _samples()
    # proposal 提交/确认/拒绝各计数
    assert _delta(before, after, "erp_proposals_submitted_total", kind="journal") == 2
    assert _delta(before, after, "erp_proposals_confirmed_total") == 1
    assert _delta(before, after, "erp_proposals_rejected_total") == 1
    # 工具时延（histogram）与结果/错误码分布
    assert _delta(before, after, "erp_tool_latency_seconds_count",
                  tool="query_balance") == 1
    assert _delta(before, after, "erp_tool_requests_total",
                  tool="query_balance", outcome="ok") == 1
    assert _delta(before, after, "erp_tool_errors_total",
                  tool="get_fee_detail", code="NOT_FOUND") == 1


def test_billing_failure_reasons_stay_distinguishable(settings):
    """缺汇率与缺规则是两条独立序列（指标 + 结构化日志），不合并为单一错误计数。"""
    before = _samples()
    collector = _JsonCollector()
    logger = logging.getLogger("erp.kernel")
    logger.addHandler(collector)
    try:
        with connection(settings) as conn:
            # 缺汇率：USD 规则能上线，但 fx_facts 没有登记汇率。
            _approve(conn, settings,
                     _rule_cfg("跨境服务费", "crossborder_order", unit_price=2,
                               currency="USD"))
            with pytest.raises(ErpError) as fx_failure:
                bill_event(conn, settings=settings, tenant_id=TENANT, event={
                    "stable_id": "fx-missing", "kind": "crossborder_order",
                    "occurred_at": "2026-11-09", "measures": {"quantity": 1}})
            # 缺规则：登记在用、但没有 active 规则的收费项。
            register_fee_item(conn, tenant_id=TENANT, fee_item="搬运费",
                              event_kind="handling")
            with pytest.raises(ErpError) as coverage_failure:
                bill_event(conn, settings=settings, tenant_id=TENANT, event={
                    "stable_id": "gap-1", "kind": "handling",
                    "occurred_at": "2026-11-09", "measures": {"quantity": 1}})
    finally:
        logger.removeHandler(collector)

    assert fx_failure.value.code == "FX_UNSOURCED"
    assert coverage_failure.value.code == "FEE_COVERAGE_MISSING"

    after = _samples()
    assert _delta(before, after, "erp_billing_failures_total",
                  reason="FX_UNSOURCED") == 1
    assert _delta(before, after, "erp_billing_failures_total",
                  reason="FEE_COVERAGE_MISSING") == 1
    # 两个原因各自成序列（不是同一个 reason="error" 桶）。
    assert _value(after, "erp_billing_failures_total", reason="FX_UNSOURCED") >= 1
    assert _value(after, "erp_billing_failures_total",
                  reason="FEE_COVERAGE_MISSING") >= 1

    billing_logs = [r for r in collector.records if r.get("event") == "billing_failed"]
    by_code = {r["error_code"]: r for r in billing_logs}
    assert set(by_code) == {"FX_UNSOURCED", "FEE_COVERAGE_MISSING"}
    assert by_code["FX_UNSOURCED"]["detail"]["currency"] == "USD"
    assert by_code["FEE_COVERAGE_MISSING"]["detail"]["fee_items"] == ["搬运费"]
    # 日志可关联但不可回读：只有 tenant_hash，没有租户明文。
    assert all("tenant_hash" in r for r in billing_logs)
    for record in collector.records:
        assert TENANT not in json.dumps(record, ensure_ascii=False)


def test_metrics_cover_fees_billed_and_recon_status_and_diffs(settings):
    before = _samples()
    with connection(settings) as conn:
        register_fee_item(conn, tenant_id=TENANT, fee_item="仓储费",
                          event_kind="storage_day")
        _approve(conn, settings, _rule_cfg("仓储费", "storage_day", unit_price=0.5))
        bill_event(conn, settings=settings, tenant_id=TENANT, event={
            "stable_id": "fees-event", "kind": "storage_day",
            "occurred_at": "2026-11-09", "measures": {"quantity": 3}})
        bill_period_snapshots(conn, settings=settings, tenant_id=TENANT,
                              period="2026-11-09",
                              snapshots=[{"fee_item": "仓储费", "as_of": "2026-11-09",
                                          "measures": {"quantity": 10}}])

    task = enqueue(settings, TENANT,
                   {"type": "inline", "records": [
                       {"stable_id": "o1", "amount": 100, "currency": "CNY",
                        "status": "posted"}]},
                   {"type": "inline", "records": [
                       {"stable_id": "o1", "amount": 110, "currency": "CNY",
                        "status": "posted"}]},
                   None)
    final = wait_for_task(settings, TENANT, task["task_id"], timeout=10.0)
    assert final["status"] == "done"
    assert final["result"]["summary"]["by_type"]["amount_mismatch"] == 1

    # 重启处置（重排）也计入指标，健康面能解释“这些遗留任务去哪了”。
    _insert_orphan(
        settings, "a" * 32, status="running",
        request={"left": {"type": "inline", "records": [
                     {"stable_id": "r1", "amount": 1, "currency": "CNY",
                      "status": "posted"}]},
                 "right": {"type": "inline", "records": [
                     {"stable_id": "r1", "amount": 1, "currency": "CNY",
                      "status": "posted"}]},
                 "retry_task_id": None})
    disposition = resume_orphans(settings, start_threads=False)
    assert [r["task_id"] for r in disposition["requeued"]] == ["a" * 32]

    after = _samples()
    # 计费笔数按路径分桶（event / period），币种是账务口径。
    assert _delta(before, after, "erp_fees_billed_total",
                  path="event", currency="CNY") == 1
    assert _delta(before, after, "erp_fees_billed_total",
                  path="period", currency="CNY") == 1
    # 对账任务终态 + 四类差异分别计数。
    assert _delta(before, after, "erp_recon_tasks_total", status="done") == 1
    assert _delta(before, after, "erp_recon_diffs_total",
                  diff_type="amount_mismatch") == 1
    # 重启处置动作。
    assert _delta(before, after, "erp_recon_restart_disposition_total",
                  action="requeued") == 1


# ---------------------------------------------------------------------------
# 脱敏（BYOK 零落库 + 6.1 禁止泄漏）
# ---------------------------------------------------------------------------

@pytest.mark.no_db
def test_logs_and_metrics_redact_credentials_dsn_and_tenant_plaintext():
    secret_tenant = TENANT
    payload = obs.log_event(
        "tool_call", tenant_id=secret_tenant,
        database_url="postgresql://leak_user:leak_pw@db.invalid:5432/leak_db",
        password="hunter2",
        note="connect via postgresql://leak_user:leak_pw@db.invalid/leak_db",
        api_key="sk-live-should-not-appear",
    )
    blob = json.dumps(payload, ensure_ascii=False)
    # 租户只以 12 位 sha256 前缀出现，明文与 tenant_id 键都不落。
    assert secret_tenant not in blob
    assert "tenant_id" not in payload
    assert payload["tenant_hash"] == obs.tenant_hash(secret_tenant)
    assert len(payload["tenant_hash"]) == 12
    # 凭据 / 连接串按「键名命中」与「值形状」双闸打码。
    assert payload["database_url"] == "[redacted]"
    assert payload["password"] == "[redacted]"
    assert payload["api_key"] == "[redacted]"
    assert payload["note"] == "[redacted-dsn]"
    for forbidden in ("hunter2", "sk-live-should-not-appear", "postgresql://",
                      "leak_user", "leak_pw", "db.invalid"):
        assert forbidden not in blob

    # redact() 返回新 dict，不改调用方对象。
    original = {"dsn": "postgresql://u:p@h/db", "tenant_id": secret_tenant, "ok": 1}
    redacted = obs.redact(original)
    assert redacted == {"dsn": "[redacted]", "tenant_hash": obs.tenant_hash(secret_tenant),
                        "ok": 1}
    assert original["tenant_id"] == secret_tenant

    # 指标面：无租户明文、无连接串、也不存在 tenant 标签。
    text = obs.render_metrics().decode()
    assert secret_tenant not in text
    assert "postgresql://" not in text
    label_keys = {key for family in text_string_to_metric_families(text)
                  for sample in family.samples for key in sample.labels}
    assert label_keys, "指标面为空，脱敏断言失去意义"
    assert not any("tenant" in key for key in label_keys)
