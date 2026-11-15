"""`ingest_event` 必须收 `provenance`（2026-11-15 生产 500 的回归钉）。

**红证（生产 pod 内实跑，MCP 形态）**：

    RAISED: ToolError  1 validation error for call[ingest_event]
    provenance  Unexpected keyword argument [type=unexpected_keyword_argument,
    input_value={'source': 'supplier-arrival-notice', …}, input_type=dict]

`_events.append_event` 一直支持 `provenance`（同进程形态也一直在传），但**工具面
没声明它** ⇒ 工具面的 inputSchema 里没有这个键，MCP 调用直接判非法。模板层的
`record_arrival_notice`（供应商到货预告，`orchestration.py`）正带着
`{"source": "supplier-arrival-notice", "arrival_ref": …, "po_ref": …, "requested_by": …}`
调它 ⇒ **生产 MCP 形态下到货预告整条业务线 500，而同进程形态一直正常**。

本文件钉两件事（用替身，不碰库）：

1. 工具面**声明**了 `provenance`（`inspect.signature` 里能看到，且默认 None）；
2. 值**真的传下去**（不是收下就丢）——`_events.append_event` 的 kwargs 里能看到它。
"""

from __future__ import annotations

import contextlib
import inspect

import pytest

from fd_erp_ledger_mcp import tools

#: 本文件只验「工具面收什么、往事件层传什么」，全部用替身（不连库）。
#: conftest 的 autouse `_reset_db` 对未标记的用例会取 `settings` fixture，
#: 而它在无 DSN 时直接 skip ⇒ 不打这个标记，这三条就白写了（「等于没验」）。
pytestmark = pytest.mark.no_db


class _RecorderMCP:
    """替 FastMCP：只把 `@mcp.tool(...)` 装饰的函数收进表里。"""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@contextlib.contextmanager
def _stub_env(monkeypatch):
    """把库/租户/事件层换成记录器：本用例只验「收什么、传什么」。"""
    recorded: dict[str, object] = {}

    @contextlib.contextmanager
    def _conn(_settings):
        yield object()

    def _resolve_tenant(_conn, _settings, tenant_id):
        return tenant_id or "t-smoke"

    def _append_event(_conn, **kwargs):
        recorded.update(kwargs)
        return {"id": 1, "status": "accepted", "provenance": kwargs.get("provenance")}

    monkeypatch.setattr(tools, "connection", _conn)
    monkeypatch.setattr(tools, "resolve_tenant", _resolve_tenant)
    monkeypatch.setattr(tools._events, "append_event", _append_event)
    yield recorded


def _ingest(monkeypatch):
    mcp = _RecorderMCP()
    settings = object()
    with _stub_env(monkeypatch) as recorded:
        tools.register(mcp, settings)  # type: ignore[arg-type]
        yield mcp.tools["ingest_event"], recorded


def test_tool_declares_provenance():
    """签名里必须有 provenance——没有它，MCP 侧 inputSchema 就不收这个键。"""
    mcp = _RecorderMCP()
    tools.register(mcp, object())  # type: ignore[arg-type]
    fn = mcp.tools["ingest_event"]
    params = inspect.signature(fn).parameters
    assert "provenance" in params, (
        "工具面没声明 provenance ⇒ MCP 调用带它会判 unexpected_keyword_argument"
        "（2026-11-15 到货预告生产 500 的直接成因）")
    assert params["provenance"].default is None


def test_provenance_is_forwarded_to_event_layer(monkeypatch):
    """收下不等于用了：值要真落到 append_event 的 kwargs 里。"""
    gen = _ingest(monkeypatch)
    fn, recorded = next(gen)
    payload = {"source": "supplier-arrival-notice", "arrival_ref": "PO-1",
               "po_ref": "PO-1", "requested_by": "staff-1"}
    out = fn("goods_received", "SMOKE-PROV-0001", "2026-11-15T00:00:00Z",
             {"sku": "X", "qty": "1"}, source_ref="PO-1", provenance=payload)

    assert recorded["provenance"] == payload, (
        f"provenance 没传下去：{recorded.get('provenance')!r}")
    assert recorded["event_type"] == "goods_received"
    assert recorded["lane"] == "ingest"
    assert out["status"] == "accepted"


def test_provenance_optional(monkeypatch):
    """不带 provenance 时不能凭空造一个（字典/None 都要如实）。"""
    gen = _ingest(monkeypatch)
    fn, recorded = next(gen)
    fn("goods_received", "SMOKE-PROV-0002", "2026-11-15T00:00:00Z",
       {"sku": "X", "qty": "1"})
    assert recorded.get("provenance") is None
