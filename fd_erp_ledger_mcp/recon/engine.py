"""对账长任务引擎（mcp-long-running-operations / 任务 4.2/4.3）。

- run_reconciliation 立即返回 task_id（queued），后台线程处理，不要求调用方同步等完。
- 状态 排队/进行中/完成/失败 + 已处理数；进度单调不倒退；逐条成败落库。
- 任务状态与结果在 DB 中（服务重启后仍可查）；启动时对遗留 running/queued 任务
  **续跑或显式重排**（optimize-erp-solution 6.5）：请求快照（0006 的 request 列）
  齐全 → 打回 queued 并重新起 worker；无可恢复快照的存量行才显式标 interrupted
  并带原因。处置结果进健康输出（startup_disposition），不静默消失。
- 仅失败条目可重试（retry_task_id）：已成功条目不重复处理、不重复计费。
- 对账只读滩头：引擎只写 recon_reports / recon_task_items / audit，
  对客户外部系统零写入（summary.external_writes=0）。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import observability as obs
from ..config import Settings
from ..db import fetch_all, fetch_one, get_pool
from ..errors import ErpError
from .adapters import build_adapter
from .align import align

_executor_lock = threading.Lock()

#: 启动处置记录（进程内，只读给 /health；不含租户明文，只报 task_id 与动作）。
_STARTUP_DISPOSITION: dict[str, Any] = {"state": "not_started"}


def startup_disposition() -> dict[str, Any]:
    """最近一次启动对账任务处置结果（/health 用；零数据库往返）。"""
    return dict(_STARTUP_DISPOSITION)


def _set_startup_disposition(value: dict[str, Any]) -> None:
    global _STARTUP_DISPOSITION
    _STARTUP_DISPOSITION = value


def mark_interrupted(settings: Settings) -> int:
    """运维显式动作：把当前 queued/running 任务全部标 interrupted。

    保留作为「不要再续跑」的强制手段；**重启路径不再调它**（那正是「静默留
    interrupted」的根因），重启走 `resume_orphans`。
    """
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE recon_reports SET status='interrupted', stage='interrupted', "
                    "error='运维显式中断；如需续跑请重新提交', updated_at=now() "
                    "WHERE status IN ('queued','running')"
                )
                return cur.rowcount
    finally:
        get_pool(settings).putconn(conn)


def _requeue_task(settings: Settings, task_id: str) -> None:
    """把任务打回 queued 并标明这是重启后的重排（留痕，不伪装成新任务）。"""
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE recon_reports SET status='queued', stage='requeued', "
                    "error='服务重启：已重新入队续跑（requeued）', updated_at=now() "
                    "WHERE task_id=%s AND status IN ('queued','running')",
                    (task_id,),
                )
    finally:
        get_pool(settings).putconn(conn)


def _mark_task_interrupted(settings: Settings, task_id: str, reason: str) -> None:
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                # 只把仍在队列/运行中的任务标为中断：带上状态条件，避免双实例启动或
                # 旧 worker 尚未退出时把已经 done 的任务改写回 interrupted。
                cur.execute(
                    "UPDATE recon_reports SET status='interrupted', stage='interrupted', "
                    "error=%s, updated_at=now() "
                    "WHERE task_id=%s AND status IN ('queued','running')",
                    (reason, task_id),
                )
    finally:
        get_pool(settings).putconn(conn)


def resume_orphans(settings: Settings, *, start_threads: bool = True) -> dict[str, Any]:
    """服务启动时调用：续跑或显式重排上次遗留的未完成对账任务（6.5）。

    - 请求快照（0006 的 recon_reports.request）齐全 → 打回 queued/stage=requeued 并
      重新起 worker；`processed` 单调、逐条 upsert 幂等，等于「续跑」而不是重算。
    - 无请求快照的存量行 → 显式标 interrupted 并写原因（如实：没快照就没法续跑），
      处置结果进 `startup_disposition()`，/health 可见，不静默缺失。
    """
    conn = get_pool(settings).getconn()
    try:
        rows = fetch_all(
            conn,
            "SELECT task_id, tenant_id, status, request FROM recon_reports "
            "WHERE status IN ('queued','running') ORDER BY created_at, task_id",
        )
    finally:
        get_pool(settings).putconn(conn)

    requeued: list[dict[str, Any]] = []
    interrupted: list[dict[str, Any]] = []
    for row in rows:
        req = row["request"] if isinstance(row["request"], dict) else None
        left = (req or {}).get("left")
        right = (req or {}).get("right")
        if isinstance(left, dict) and isinstance(right, dict):
            _requeue_task(settings, row["task_id"])
            if start_threads:
                args = (settings, row["task_id"], row["tenant_id"], left, right,
                        (req or {}).get("retry_task_id"))
                threading.Thread(target=_run_worker, args=args,
                                 name=f"recon-resume-{row['task_id'][:8]}", daemon=True).start()
            requeued.append({"task_id": row["task_id"], "action": "requeued"})
            obs.recon_restart_disposition("requeued", task_id=row["task_id"])
        else:
            reason = ("遗留任务缺少请求快照（0006 之前创建），无法续跑；"
                      "已显式标 interrupted，请重新提交")
            _mark_task_interrupted(settings, row["task_id"], reason)
            interrupted.append({"task_id": row["task_id"], "action": "interrupted",
                                "reason": reason})
            obs.recon_restart_disposition("interrupted", task_id=row["task_id"],
                                          reason="no_request_snapshot")

    disposition = {
        "state": "done",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "found": len(rows),
        "requeued": requeued,
        "interrupted": interrupted,
    }
    _set_startup_disposition(disposition)
    obs.log_event("recon_startup_disposition", found=len(rows),
                  requeued=len(requeued), interrupted=len(interrupted))
    return disposition


def enqueue(settings: Settings, tenant_id: str, left_spec: dict[str, Any],
            right_spec: dict[str, Any], retry_task_id: str | None) -> dict[str, Any]:
    """创建任务行（queued），启动后台线程，立即返回 task_id。"""
    task_id = uuid.uuid4().hex
    request = {"left": left_spec, "right": right_spec, "retry_task_id": retry_task_id,
               "enqueued_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO recon_reports(task_id, tenant_id, status, stage, total, request) "
                    "VALUES (%s,%s,'queued','queued',0,%s::jsonb)",
                    (task_id, tenant_id, json.dumps(request, ensure_ascii=False)),
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
        # 内核内部两型（events / posting）需要一条读连接；连接由引擎注入，
        # 不接受请求里自带 —— 否则调用方可以指定去对哪本账。
        conn_for_sources = get_pool(settings).getconn()
        try:
            left_adapter = build_adapter({**left_spec, "_conn": conn_for_sources}, "left")
            right_adapter = build_adapter({**right_spec, "_conn": conn_for_sources}, "right")
            left_records, left_failures = left_adapter.read_tolerant()
            right_records, right_failures = right_adapter.read_tolerant()
        finally:
            get_pool(settings).putconn(conn_for_sources)

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
        obs.recon_task_finished("done", tenant_id=tenant_id,
                                diffs_by_type=outcome.get("summary", {}).get("by_type"))
    except ErpError as exc:
        _set_progress(settings, task_id, status="failed", stage="failed", error=exc.code)
        obs.recon_task_finished("failed", tenant_id=tenant_id)
    except Exception as exc:  # pragma: no cover - 防御
        _set_progress(settings, task_id, status="failed", stage="failed",
                      error=f"INTERNAL: {exc}")
        obs.recon_task_finished("failed", tenant_id=tenant_id)


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
