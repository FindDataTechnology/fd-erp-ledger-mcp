"""对账长任务引擎（mcp-long-running-operations / 任务 4.2/4.3）。

- run_reconciliation 立即返回 task_id（queued），后台线程处理，不要求调用方同步等完。
- 状态 排队/进行中/完成/失败 + 已处理数；进度单调不倒退；逐条成败落库。
- 任务状态与结果在 DB 中（服务重启后仍可查）；启动时把遗留 running/queued
  如实标记为 interrupted 并说明需重新提交（不伪装成「不存在」）。
- 仅失败条目可重试（retry_task_id）：已成功条目不重复处理、不重复计费。
- 对账只读滩头：引擎只写 recon_reports / recon_task_items / audit，
  对客户外部系统零写入（summary.external_writes=0）。
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from ..config import Settings
from ..db import fetch_one, get_pool
from ..errors import ErpError
from .adapters import build_adapter
from .align import align

_executor_lock = threading.Lock()


def mark_interrupted(settings: Settings) -> int:
    """服务启动时调用：历史 running/queued 任务如实转 interrupted。"""
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE recon_reports SET status='interrupted', stage='interrupted', "
                    "error='服务重启，任务不可恢复；请重新提交', updated_at=now() "
                    "WHERE status IN ('queued','running')"
                )
                return cur.rowcount
    finally:
        get_pool(settings).putconn(conn)


def enqueue(settings: Settings, tenant_id: str, left_spec: dict[str, Any],
            right_spec: dict[str, Any], retry_task_id: str | None) -> dict[str, Any]:
    """创建任务行（queued），启动后台线程，立即返回 task_id。"""
    task_id = uuid.uuid4().hex
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO recon_reports(task_id, tenant_id, status, stage, total) "
                    "VALUES (%s,%s,'queued','queued',0)",
                    (task_id, tenant_id),
                )
    finally:
        get_pool(settings).putconn(conn)

    args = (settings, task_id, tenant_id, left_spec, right_spec, retry_task_id)
    th = threading.Thread(target=_run_worker, args=args, name=f"recon-{task_id[:8]}",
                          daemon=True)
    th.start()
    return {"task_id": task_id, "status": "queued",
            "poll_with": "get_recon_report", "retry_task_id": retry_task_id}


def _set_progress(settings: Settings, task_id: str, *, status: str | None = None,
                  stage: str | None = None, processed: int | None = None,
                  total: int | None = None, result: dict | None = None,
                  error: str | None = None) -> None:
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE recon_reports SET "
                    "status=coalesce(%s,status), stage=coalesce(%s,stage), "
                    "processed=greatest(processed, %s), total=greatest(total, %s), "
                    "result=coalesce(%s::jsonb,result), error=coalesce(%s,error), "
                    "updated_at=now() WHERE task_id=%s",
                    (status, stage, processed if processed is not None else 0,
                     total if total is not None else 0,
                     json.dumps(result, ensure_ascii=False) if result is not None else None,
                     error, task_id),
                )
    finally:
        get_pool(settings).putconn(conn)


def _run_worker(settings: Settings, task_id: str, tenant_id: str,
                left_spec: dict, right_spec: dict, retry_task_id: str | None) -> None:
    try:
        _set_progress(settings, task_id, status="running", stage="reading_sources")
        left_adapter = build_adapter(left_spec, "left")
        right_adapter = build_adapter(right_spec, "right")
        left_records, left_failures = left_adapter.read_tolerant()
        right_records, right_failures = right_adapter.read_tolerant()

        # 仅失败条目重试：从上一任务取失败 item_key，本轮只重放这些条目。
        retry_keys: set[str] | None = None
        if retry_task_id:
            conn = get_pool(settings).getconn()
            try:
                rows = conn.execute(
                    "SELECT item_key FROM recon_task_items "
                    "WHERE task_id=%s AND status='failed'", (retry_task_id,),
                ).fetchall()
                retry_keys = {r[0] for r in rows}
            finally:
                get_pool(settings).putconn(conn)

        all_items = {r.stable_id for r in left_records} | {r.stable_id for r in right_records}
        if retry_keys is not None:
            all_items &= retry_keys  # 已成功条目不重复处理
        total = len(all_items)
        _set_progress(settings, task_id, stage="aligning", total=total, processed=0)

        outcome = align(left_records, right_records)
        failures = left_failures + right_failures
        failed_keys = {f["item_key"] for f in failures}
        # 逐条成败落库（processed 单调推进）
        conn = get_pool(settings).getconn()
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    done = 0
                    for sid in sorted(all_items):
                        st = "failed" if sid in failed_keys else "ok"
                        reason = next((f["reason"] for f in failures
                                       if f["item_key"] == sid), None)
                        cur.execute(
                            "INSERT INTO recon_task_items(task_id,item_key,status,reason) "
                            "VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT (task_id,item_key) DO UPDATE SET "
                            "status=excluded.status, reason=excluded.reason, updated_at=now()",
                            (task_id, sid, st, reason),
                        )
                        done += 1
                        if done % 10 == 0:  # 进度不只在结束时给
                            _set_progress(settings, task_id, processed=done)
                    for f in failures:
                        cur.execute(
                            "INSERT INTO recon_task_items(task_id,item_key,status,reason) "
                            "VALUES (%s,%s,'failed',%s) "
                            "ON CONFLICT (task_id,item_key) DO NOTHING",
                            (task_id, f["item_key"], f["reason"]),
                        )
        finally:
            get_pool(settings).putconn(conn)

        report = {
            **outcome,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "retry_of": retry_task_id,
            "failed_items": failures,
            "left_adapter": left_adapter.describe(),
            "right_adapter": right_adapter.describe(),
        }
        _set_progress(settings, task_id, status="done", stage="done",
                      processed=total, total=total, result=report)
    except ErpError as exc:
        _set_progress(settings, task_id, status="failed", stage="failed", error=exc.code)
    except Exception as exc:  # pragma: no cover - 防御
        _set_progress(settings, task_id, status="failed", stage="failed",
                      error=f"INTERNAL: {exc}")


def get_report(settings: Settings, tenant_id: str, task_id: str) -> dict[str, Any]:
    conn = get_pool(settings).getconn()
    try:
        row = fetch_one(
            conn,
            "SELECT task_id, tenant_id, status, stage, processed, total, result, error, "
            "created_at, updated_at FROM recon_reports WHERE task_id=%s AND tenant_id=%s",
            (task_id, tenant_id),
        )
    finally:
        get_pool(settings).putconn(conn)
    if row is None:
        raise ErpError("RECON_TASK_NOT_FOUND", f"对账任务不存在：{task_id}",
                       detail={"task_id": task_id})
    for k in ("created_at", "updated_at"):
        row[k] = str(row[k])
    return row


def wait_for_task(settings: Settings, tenant_id: str, task_id: str,
                  timeout: float = 10.0, interval: float = 0.02) -> dict[str, Any]:
    """供 e2e/测试等待任务收敛（生产调用方无需用，轮询 get_recon_report 即可）。"""
    import time
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = get_report(settings, tenant_id, task_id)
        if last["status"] in ("done", "failed", "interrupted"):
            return last
        time.sleep(interval)
    raise AssertionError(f"对账任务未在 {timeout}s 内收敛：{last}")
