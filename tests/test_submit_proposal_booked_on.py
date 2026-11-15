"""`submit_proposal` 必须收 `booked_on`（2026-11-15 第三个「工具面比调用方窄」缺陷）。

**红证（修前，MCP 形态本机直接调用）**：

    TypeError: McpKernelClient.submit_proposal() got an unexpected keyword argument 'booked_on'

`proposals.submit_proposal` 一直有 `booked_on`（业务记账日），同进程形态也一直在传，
但**工具面没声明它** ⇒ 工具面的 inputSchema 里没有这个键。模板编排有 14 处调用点带它
（期初/结账/坏账/申报/发票/税务/合并/汇兑重估/制造业），生产 api 跑
`ERP_KERNEL_TRANSPORT=mcp` ⇒ 这些提交路径在生产形态下必炸。

与 `tests/test_ingest_provenance.py` 是同一族的两个实例：**内部函数收、工具面不收**。
两者的共同判据已收进契约闸（`fd-erp/tools/check_kernel_client_contract.py` 的
`TRANSPORT_PARAM_MISSING` + `CALLSITE_UNKNOWN_KWARG`），本文件是工具面这一侧的钉子。

钉三件事（用替身，不碰库）：

1. 签名里有 `booked_on` 且默认 None；
2. 值真传给 `proposals.submit_proposal`；
3. 不给时如实 None（不替调用方编一个业务日期）。
"""

from __future__ import annotations

import contextlib
import inspect

import pytest

from fd_erp_ledger_mcp import tools

#: 与 test_ingest_provenance 同理：conftest 的 autouse `_reset_db` 会取 `settings`
#: fixture，无 DSN 时直接 skip ⇒ 不打这个标记，这三条就白写了。
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
    recorded: dict[str, object] = {}

    @contextlib.contextmanager
    def _conn(_settings):
        yield object()

    def _resolve_tenant(_conn, _settings, tenant_id):
        return tenant_id or "t-smoke"

    def _submit(_conn, **kwargs):
        recorded.update(kwargs)
        return {"proposal_id": 1, "status": "pending"}

    monkeypatch.setattr(tools, "connection", _conn)
    monkeypatch.setattr(tools, "resolve_tenant", _resolve_tenant)
    monkeypatch.setattr(tools, "_submit", _submit)
    yield recorded


def test_tool_declares_booked_on():
    """签名里必须有 booked_on——没有它，MCP 侧 inputSchema 就不收这个键。"""
    mcp = _RecorderMCP()
    tools.register(mcp, object())  # type: ignore[arg-type]
    params = inspect.signature(mcp.tools["submit_proposal"]).parameters
    assert "booked_on" in params, (
        "工具面没声明 booked_on ⇒ 模板 14 处调用点在 MCP 形态下 TypeError"
        "（与 ingest_event.provenance 同族）")
    assert params["booked_on"].default is None


def test_booked_on_is_forwarded_to_proposals(monkeypatch):
    """收下不等于用了：值要真落到 proposals.submit_proposal 的 kwargs 里。"""
    mcp = _RecorderMCP()
    with _stub_env(monkeypatch) as recorded:
        tools.register(mcp, object())  # type: ignore[arg-type]
        mcp.tools["submit_proposal"](
            "exp:2026-11", [{"account_code": "5200", "direction": "debit",
                             "amount": "1"}],
            source_ref="opening", booked_on="2026-11-15")
    assert recorded["booked_on"] == "2026-11-15", (
        f"booked_on 没传下去：{recorded.get('booked_on')!r}")
    assert recorded["stable_id"] == "exp:2026-11"


def test_booked_on_optional(monkeypatch):
    """不给就是 None：内核按事件时刻推，不补默认业务日期。"""
    mcp = _RecorderMCP()
    with _stub_env(monkeypatch) as recorded:
        tools.register(mcp, object())  # type: ignore[arg-type]
        mcp.tools["submit_proposal"]("exp:2026-12", [])
    assert recorded.get("booked_on") is None
