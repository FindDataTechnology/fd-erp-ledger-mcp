"""e2e 全链（6.2）：proposal → 确认 → 落账 → 余额 → 对账 → 冲正，可计数验收。

同时覆盖 MCP 工具面握手（8 工具全暴露）+ 工具层 rows_returned 落戳。
"""
from __future__ import annotations

import pytest
from fastmcp import Client

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import connection
from fd_erp_ledger_mcp.fx import seed_fx_facts
from fd_erp_ledger_mcp.ledger import compute_balance, query_entries
from fd_erp_ledger_mcp.proposals import confirm_proposal, submit_proposal

EXPECTED_TOOLS = {
    "submit_proposal", "list_proposals", "confirm_proposal", "reject_proposal",
    "query_balance", "query_entries", "run_reconciliation", "get_recon_report",
}


def test_create_server_zero_config_lists_eight_tools():
    """零配置 create_server 可握手；工具面恰为契约 8 个。"""
    from fd_erp_ledger_mcp.server import create_server
    server = create_server(Settings(database_url=None))

    async def go():
        async with Client(server) as client:
            tools = await client.list_tools()
            return {t.name for t in tools}

    import asyncio
    names = asyncio.run(go())
    assert names == EXPECTED_TOOLS


def test_tool_layer_rows_returned_stamp(settings):
    """工具层：成功响应必带 rows_returned（写工具落 0）；失败带 code 不伪装成功。"""
    from fd_erp_ledger_mcp.server import create_server
    server = create_server(settings)

    async def go():
        async with Client(server) as client:
            ok = await client.call_tool("submit_proposal", {
                "stable_id": "tool-1",
                "lines": [
                    {"account_code": "cash", "direction": "debit", "amount": 10},
                    {"account_code": "revenue", "direction": "credit", "amount": 10},
                ],
            })
            bal = await client.call_tool("query_balance", {})
            return ok, bal

    import asyncio
    ok, bal = asyncio.run(go())
    ok_data = ok.data if hasattr(ok, "data") else ok
    assert ok_data["rows_returned"] == 0
    assert ok_data["ok"] is True
    bal_data = bal.data if hasattr(bal, "data") else bal
    assert bal_data["rows_returned"] == 0  # 无分录
    assert bal_data["data"] == []


def test_full_chain_countable(settings):
    """proposal → 确认 → 落账 → 余额 → 对账 → 冲正 全链计数验收。"""
    from fd_erp_ledger_mcp.recon.engine import enqueue, wait_for_task

    with connection(settings) as conn:
        seed_fx_facts(conn, [
            {"quote_currency": "USD", "rate": "7.0", "as_of": "2026-11-07", "source": "ecb-exr"},
        ], settings=settings)
        # 1) proposal（USD 订单，双记）
        p = submit_proposal(
            conn, settings=settings, tenant_id="acme", stable_id="e2e-1",
            lines=[
                {"account_code": "cash", "direction": "debit", "amount": 100, "currency": "USD",
                 "fx_rate": "7.0", "fx_as_of": "2026-11-07", "fx_source": "ecb-exr"},
                {"account_code": "revenue", "direction": "credit", "amount": 100, "currency": "USD",
                 "fx_rate": "7.0", "fx_as_of": "2026-11-07", "fx_source": "ecb-exr"},
            ])
        assert p["status"] == "pending"
        assert query_entries(conn, "acme") == []
        # 2) 确认落账
        c = confirm_proposal(conn, settings=settings, tenant_id="acme", stable_id="e2e-1")
        assert c["status"] == "confirmed"
        assert len(query_entries(conn, "acme")) == 2
        # 3) 余额
        bal = {r["account_code"]: r["balance"]
               for r in compute_balance(conn, "acme", None, settings=settings)}
        assert bal["cash"] == "700.000000"
        # 4) 对账（两源含一笔金额不符）
        task = enqueue(
            settings, "acme",
            {"type": "inline", "records": [
                {"stable_id": "o1", "amount": 100, "currency": "CNY", "status": "paid"},
                {"stable_id": "o2", "amount": 200, "currency": "CNY", "status": "paid"},
            ]},
            {"type": "inline", "records": [
                {"stable_id": "o1", "amount": 100, "currency": "CNY", "status": "paid"},
                {"stable_id": "o2", "amount": 210, "currency": "CNY", "status": "paid"},
            ]},
            None,
        )
        rep = wait_for_task(settings, "acme", task["task_id"], timeout=10.0)
        assert rep["status"] == "done"
        assert rep["result"]["summary"]["by_type"]["amount_mismatch"] == 1
        assert rep["result"]["summary"]["matched"] == 1
        assert rep["result"]["summary"]["external_writes"] == 0
        # 5) 冲正
        submit_proposal(conn, settings=settings, tenant_id="acme", stable_id="e2e-rev",
                        kind="reversal", reversal_of=c["txn_id"], lines=[])
        confirm_proposal(conn, settings=settings, tenant_id="acme", stable_id="e2e-rev")
        assert len(query_entries(conn, "acme")) == 4
        bal2 = {r["account_code"]: r["balance"]
                for r in compute_balance(conn, "acme", None, settings=settings)}
        assert bal2["cash"] == "0.000000"
