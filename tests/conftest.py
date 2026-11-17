"""测试夹具：真实 Postgres（DB 约束层不可 mock）。

ERP_LEDGER_DATABASE_URL 指向一次性测试库；migrate 一次，逐用例 TRUNCATE 隔离。
"""
from __future__ import annotations

import os

import psycopg
import pytest

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import create_schema, migrate
from fd_erp_ledger_mcp.tenancy import schema_for

pytest_plugins = ()

TENANT_ID = "acme"


@pytest.fixture(scope="session")
def settings():
    dsn = os.environ.get("ERP_LEDGER_DATABASE_URL")
    if not dsn:
        pytest.skip("ERP_LEDGER_DATABASE_URL 未设置（需真实 Postgres）")
    schema = schema_for(TENANT_ID)
    # schema 隔离（erp-tenant-schema-isolation）：租户 "acme" 现在住在独立 schema
    # `tenant_acme` 里，而不是在 public 里靠 tenant_id 列区分。
    #
    # 把 schema 写进**夹具给的 DSN**，是让整个既有套件（几十处裸
    # `psycopg.connect(settings.database_url)`）自动落到本租户 schema 的唯一办法：
    # 生产里这件事由连接池 kwargs 承担，测试里这些裸连接没有池，只能靠 DSN 上的
    # `options`。逐处改调用点既容易漏、也会让「漏了的那处回到 public」变成静默行为。
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params["options"] = f"-c search_path={schema}"
    s = Settings(database_url=make_conninfo(**params), tenant_id=TENANT_ID,
                 base_currency="CNY")
    # `public` 情形被 create_schema 短路、不发任何 DDL——生产路径的「零变更」
    # 靠的就是这条短路，不是「碰巧没执行到」。
    create_schema(s)
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
    # 清空必须落在**本租户的 schema** 里：不设 search_path 的裸连接默认解析到 public，
    # 那样清的是租户 0 的表，而用例读写的却是 tenant_acme —— 表现为「清了但还是脏」，
    # 且会顺带把另一个租户的数据清掉。
    conn = psycopg.connect(
        settings.database_url, autocommit=True,
        options=f"-c search_path={schema_for(settings.tenant_id)}",
    )
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
