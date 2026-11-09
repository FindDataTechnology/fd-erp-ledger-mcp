"""对账任务重启续跑（optimize-erp-solution 6.5 / erp-operations-hardening「对账任务重启可续跑」）。

验收口径：
  * 上次运行遗留的 queued/running 任务，在「进程重启」路径（resume_orphans）下
    **续跑或显式重排**，并留下可查的痕（DB stage/error + 结构化日志）；
  * 没有请求快照（0006 之前创建）的存量遗留任务无法续跑，必须显式标 interrupted
    并把原因写清楚，不得静默消失；
  * 处置结果出现在 `/health` 的 `recon_startup` 字段（按 server.py 实际字段断言），
    且健康输出不回显租户明文；
  * `mark_interrupted` 保留为「不要再续跑」的运维显式动作，既有用例继续通过。

真库：任务状态与请求快照都在库里，重启语义（谁能续跑）不是内存状态能模拟的。
"""
from __future__ import annotations

import asyncio
import json
import logging

import psycopg

from fd_erp_ledger_mcp.recon.engine import (get_report, mark_interrupted,
                                            resume_orphans, wait_for_task)
from fd_erp_ledger_mcp.server import create_server

#: 故意取一个可识别的租户名：健康输出/日志若回显明文，断言能抓住。
TENANT = "tenant-restart-probe"

RESUMABLE_TASK = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
LEGACY_TASK = "ffeeddccbbaa99887766554433221100"


def _request() -> dict:
    return {
        "left": {"type": "inline", "records": [
            {"stable_id": "o1", "amount": 100, "currency": "CNY", "status": "posted"}]},
        "right": {"type": "inline", "records": [
            {"stable_id": "o1", "amount": 100, "currency": "CNY", "status": "posted"}]},
        "retry_task_id": None,
    }


def _insert_leftover(settings, task_id: str, *, status: str,
                     request: dict | None) -> None:
    """直接插行模拟「上个进程留下的任务」——不经过 enqueue，避免 worker 抢先跑完。"""
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


def _health_payload(settings) -> dict:
    app = create_server(settings).http_app()
    route = next((r for r in app.routes if getattr(r, "path", "") == "/health"), None)
    assert route is not None, "/health 路由未注册"
    return json.loads(asyncio.run(route.endpoint(None)).body)


def test_resume_orphans_resumes_resumable_and_interrupts_legacy(settings):
    """有请求快照 → 重排并真的跑完；无快照 → 显式 interrupted 带原因。"""
    _insert_leftover(settings, RESUMABLE_TASK, status="running", request=_request())
    _insert_leftover(settings, LEGACY_TASK, status="queued", request=None)

    collector_infra: list[dict] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                collector_infra.append(json.loads(record.getMessage()))
            except (TypeError, ValueError):
                collector_infra.append({"raw": record.getMessage()})

    logger = logging.getLogger("erp.kernel")
    handler = _Collector()
    logger.addHandler(handler)
    try:
        disposition = resume_orphans(settings, start_threads=True)
    finally:
        logger.removeHandler(handler)

    # 处置记录：两类任务被分开处理，不静默。
    assert disposition["state"] == "done"
    assert disposition["found"] == 2
    assert [r["task_id"] for r in disposition["requeued"]] == [RESUMABLE_TASK]
    assert disposition["requeued"][0]["action"] == "requeued"
    assert [r["task_id"] for r in disposition["interrupted"]] == [LEGACY_TASK]
    assert "缺少请求快照" in disposition["interrupted"][0]["reason"]

    # 续跑：任务真的执行完（不是只把状态改回 queued 就停在那里）。
    final = wait_for_task(settings, TENANT, RESUMABLE_TASK, timeout=10.0)
    assert final["status"] == "done"
    assert final["stage"] == "done"
    assert final["result"]["summary"]["matched"] == 1
    assert final["result"]["summary"]["external_writes"] == 0
    # 重排留痕：error 列保留「重启后重新入队」，没有被完成状态洗掉。
    assert "requeued" in (final["error"] or "")

    # 不可恢复的遗留任务：interrupted + 原因（truely unable，而不是假装续跑）。
    legacy = get_report(settings, TENANT, LEGACY_TASK)
    assert legacy["status"] == "interrupted"
    assert legacy["stage"] == "interrupted"
    assert "无法续跑" in legacy["error"] and "请求快照" in legacy["error"]

    # 结构化日志留痕：每个遗留任务一条处置记录 + 一条汇总。
    actions = {(r.get("task_id"), r.get("action"))
               for r in collector_infra if r.get("event") == "recon_restart_disposition"}
    assert (RESUMABLE_TASK, "requeued") in actions
    assert (LEGACY_TASK, "interrupted") in actions
    summary = [r for r in collector_infra if r.get("event") == "recon_startup_disposition"]
    assert len(summary) == 1
    assert (summary[0]["found"], summary[0]["requeued"], summary[0]["interrupted"]) == (2, 1, 1)
    # 日志不含租户明文（这些处置日志本来就不带租户，只报 task_id 与动作）。
    for record in collector_infra:
        assert TENANT not in json.dumps(record, ensure_ascii=False)


def test_health_exposes_startup_disposition(settings):
    """`/health.recon_startup` 能看到遗留任务及其处置结果，且不回显租户明文。"""
    _insert_leftover(settings, RESUMABLE_TASK, status="queued", request=_request())
    _insert_leftover(settings, LEGACY_TASK, status="running", request=None)

    disposition = resume_orphans(settings, start_threads=False)
    assert disposition["found"] == 2

    payload = _health_payload(settings)
    assert payload["ok"] is True
    assert payload["service"] == "fd-erp-ledger-mcp"
    assert payload["kernel_transport"] == "mcp"
    startup = payload["recon_startup"]
    # 字段按 server.py 实际输出的 disposition 断言。
    assert set(startup) == {"state", "at", "found", "requeued", "interrupted"}
    assert startup["state"] == "done"
    assert startup["found"] == 2
    assert [r["task_id"] for r in startup["requeued"]] == [RESUMABLE_TASK]
    assert [r["task_id"] for r in startup["interrupted"]] == [LEGACY_TASK]
    assert startup["interrupted"][0]["reason"]

    blob = json.dumps(payload, ensure_ascii=False)
    assert TENANT not in blob
    assert "postgresql://" not in blob


def test_mark_interrupted_remains_explicit_ops_action(settings):
    """重启路径不再调它；`mark_interrupted` 仍是运维「全部中断」的显式手段。"""
    _insert_leftover(settings, RESUMABLE_TASK, status="queued", request=_request())
    _insert_leftover(settings, LEGACY_TASK, status="running", request=_request())

    assert mark_interrupted(settings) == 2

    for task_id in (RESUMABLE_TASK, LEGACY_TASK):
        row = get_report(settings, TENANT, task_id)
        assert row["status"] == "interrupted"
        assert row["stage"] == "interrupted"
        assert row["error"] and "运维显式中断" in row["error"]
