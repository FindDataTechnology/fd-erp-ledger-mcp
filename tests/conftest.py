"""测试夹具：真实 Postgres（DB 约束层不可 mock）。

ERP_LEDGER_DATABASE_URL 指向一次性测试库；migrate 一次，逐用例 TRUNCATE 隔离。
"""
from __future__ import annotations

import os

import psycopg
import pytest

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import migrate

pytest_plugins = ()


@pytest.fixture(scope="session")
def settings():
    dsn = os.environ.get("ERP_LEDGER_DATABASE_URL")
    if not dsn:
        pytest.skip("ERP_LEDGER_DATABASE_URL 未设置（需真实 Postgres）")
    s = Settings(database_url=dsn, tenant_id="acme", base_currency="CNY")
    migrate(s)  # 一次性、可重放
    return s


@pytest.fixture(autouse=True)
def _reset_db(request):
    """逐用例清空业务表；journal_entries 的 TRUNCATE 需维护会话开关。

    带 `no_db` 标记的用例跳过本前置（harden-erp-operations 4.1）：承载探针这类
    零依赖用例被整体 skip 掉，等于没验——2026-11-08 实测 `2 skipped` 后才加这条。
    """
    if request.node.get_closest_marker("no_db"):
        yield
        return
    settings = request.getfixturevalue("settings")
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('erp.allow_maintenance','on', true)")
                cur.execute(
                    "TRUNCATE proposals, journal_entries, fx_facts, recon_reports, "
                    "recon_task_items, audit, accounts, billing_rules, fee_details, "
                    "billing_fee_items, credit_registry, credit_reservations, "
                    "posting_gaps, posting_links, posting_rules, "
                    # `objects` 必须在这里：漏了它，上一轮（或同一库上另一个套件）投影出来的
                    # 对象会留下来，而 `SELECT ... FROM objects` 不带 ORDER BY ——
                    # 于是「只有一行」的断言按查询顺序时绿时红（11-13 实测一次假红）。
                    "objects, object_balances, object_events, business_events, "
                    "event_revisions, accounting_periods "
                    "RESTART IDENTITY CASCADE"
                )
    finally:
        conn.close()
    yield
