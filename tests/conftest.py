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
def _reset_db(settings):
    """逐用例清空业务表；journal_entries 的 TRUNCATE 需维护会话开关。"""
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('erp.allow_maintenance','on', true)")
                cur.execute(
                    "TRUNCATE proposals, journal_entries, fx_facts, recon_reports, "
                    "recon_task_items, audit, accounts, billing_rules, fee_details, "
                    "credit_registry, credit_reservations RESTART IDENTITY CASCADE"
                )
    finally:
        conn.close()
    yield
