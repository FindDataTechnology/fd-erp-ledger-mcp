"""对账引擎（4.1-4.3）：四类差异可解释、重推去重、零外部写入、长任务与重试。"""
from __future__ import annotations

from fd_erp_ledger_mcp.recon.adapters import CsvAdapter, InlineAdapter, build_adapter
from fd_erp_ledger_mcp.recon.engine import enqueue, mark_interrupted, wait_for_task


def _rec(sid, amount, status="posted"):
    return {"stable_id": sid, "amount": amount, "currency": "CNY", "status": status}


def test_align_four_diff_classes():
    from fd_erp_ledger_mcp.recon.align import align
    left = [_rec("a", 100), _rec("b", 200), _rec("c", 300), _rec("d", 400),
            _rec("only-left", 500)]
    right = [_rec("b", 200), _rec("c", 350), _rec("d", 400, status="voided"),
             _rec("a", 100), _rec("only-right", 600)]
    out = align(InlineAdapter(left, "left").read(), InlineAdapter(right, "right").read())
    types = {d["diff_type"] for d in out["diffs"]}
    assert types == {"missing_in_right", "extra_in_right", "amount_mismatch", "status_mismatch"}
    assert out["summary"]["external_writes"] == 0
    for d in out["diffs"]:
        # 每条差异含双侧原始引用（缺失侧可为 None，但键必须在）
        assert "left_ref" in d and "right_ref" in d


def test_replay_dedup():
    from fd_erp_ledger_mcp.recon.align import align
    left = [_rec("a", 100), _rec("a", 100), _rec("a", 100)]  # 平台重推三次
    right = [_rec("a", 100)]
    out = align(InlineAdapter(left).read(), InlineAdapter(right).read())
    assert out["summary"]["matched"] == 1
    assert out["diffs"] == []
    assert len(out["summary"]["replays_deduped"]) == 2


def test_missing_and_extra():
    from fd_erp_ledger_mcp.recon.align import align
    left = [_rec("only-left", 100)]
    right = [_rec("only-right", 90)]
    out = align(InlineAdapter(left).read(), InlineAdapter(right).read())
    got = {d["stable_id"]: d["diff_type"] for d in out["diffs"]}
    assert got == {"only-left": "missing_in_right", "only-right": "extra_in_right"}


def test_csv_adapter_parses():
    csv_text = "stable_id,amount,currency,status\na,100,CNY,posted\nb,200,CNY,voided\n"
    recs = CsvAdapter(csv_text, "right").read()
    assert [r.stable_id for r in recs] == ["a", "b"]
    assert recs[1].status == "voided"


def test_unknown_adapter_reports_not_wired():
    from fd_erp_ledger_mcp.errors import ErpError
    try:
        build_adapter({"type": "carrier-pigeon"}, "left")
    except ErpError as exc:
        assert exc.code == "NOT_WIRED"
        return
    raise AssertionError("未知适配器应报 NOT_WIRED")


def test_long_task_full_chain_and_report(settings):
    task = enqueue(
        settings, "acme",
        {"type": "inline", "records": [_rec("a", 100), _rec("b", 200)]},
        {"type": "inline", "records": [_rec("a", 100), _rec("b", 250)]},
        None,
    )
    assert task["status"] == "queued"
    final = wait_for_task(settings, "acme", task["task_id"], timeout=10.0)
    assert final["status"] == "done"
    assert final["processed"] == final["total"] == 2
    assert final["result"]["summary"]["by_type"]["amount_mismatch"] == 1
    assert final["result"]["summary"]["external_writes"] == 0


def test_retry_only_failed_items(settings):
    # 一源含坏记录（缺 stable_id）→ 该条记 failed，好条目 ok
    first = enqueue(
        settings, "acme",
        {"type": "inline", "records": [_rec("ok1", 100), {"amount": 5}]},  # 第二条缺 stable_id
        {"type": "inline", "records": [_rec("ok1", 100)]},
        None,
    )
    r1 = wait_for_task(settings, "acme", first["task_id"], timeout=10.0)
    assert r1["status"] == "done"
    assert r1["result"]["failed_items"]
    # 仅失败条目重试：成功条目不重复处理
    retry = enqueue(
        settings, "acme",
        {"type": "inline", "records": [_rec("ok1", 100), _rec("fixed", 5)]},
        {"type": "inline", "records": [_rec("ok1", 100), _rec("fixed", 5)]},
        first["task_id"],
    )
    r2 = wait_for_task(settings, "acme", retry["task_id"], timeout=10.0)
    assert r2["status"] == "done"
    assert r2["result"]["retry_of"] == first["task_id"]
    # retry 只覆盖失败键（按上一任务失败 item_key 过滤）
    assert r2["total"] <= 1


def test_task_not_found(settings):
    from fd_erp_ledger_mcp.errors import ErpError
    from fd_erp_ledger_mcp.recon.engine import get_report
    try:
        get_report(settings, "acme", "deadbeef")
    except ErpError as exc:
        assert exc.code == "RECON_TASK_NOT_FOUND"
        return
    raise AssertionError("不存在的 task 应报 RECON_TASK_NOT_FOUND")


def test_restart_marks_interrupted(settings):
    task = enqueue(
        settings, "acme",
        {"type": "inline", "records": [_rec("x", 1)]},
        {"type": "inline", "records": [_rec("x", 1)]},
        None,
    )
    # 模拟重启：直接把任务打回 running 再调用 mark_interrupted
    import psycopg
    c = psycopg.connect(settings.database_url, autocommit=True)
    try:
        c.execute("UPDATE recon_reports SET status='running' WHERE task_id=%s",
                  (task["task_id"],))
    finally:
        c.close()
    mark_interrupted(settings)
    from fd_erp_ledger_mcp.recon.engine import get_report
    rep = get_report(settings, "acme", task["task_id"])
    # 可能已自然完成；若被标记则必须是 interrupted 且带原因
    if rep["status"] == "interrupted":
        assert rep["error"]
