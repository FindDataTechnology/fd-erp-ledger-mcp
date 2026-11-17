"""双租户真库隔离验收（openspec: erp-tenant-schema-isolation 4.1 / 4.2）。

本 change 的可信度最终落在这里：两个租户在同一 Postgres 实例上各自建 schema、
各写一笔分录，然后互相查——查不到对方的数据。离线用例（
`test_isolation_not_dependent_on_sql.py`）钉住的是「机制」，本文件钉住的是
「机制在真库上确实生效」。

**不用 `settings` 夹具**：那个夹具指向 `tenant_acme` 并在每个用例前 TRUNCATE 它。
本文件自建两个**独立** schema（`tenant_iso_a` / `tenant_iso_b`），用后即删，
既不污染 acme，也不受它的清理节奏影响。
"""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import close_pool, create_schema, drop_schema, get_pool, migrate
from fd_erp_ledger_mcp.tenancy import schema_for

pytestmark = pytest.mark.no_db  # 不走 settings/TRUNCATE 夹具；本文件自己管库


def _dsn() -> str:
    dsn = os.environ.get("ERP_LEDGER_DATABASE_URL")
    if not dsn:
        pytest.skip("ERP_LEDGER_DATABASE_URL 未设置（需真实 Postgres）")
    return dsn


def _settings_for(tenant: str) -> Settings:
    """按租户派生 Settings，并把 search_path 写进 DSN（同 conftest 的做法）。

    裸连接（本文件里验证用的 psql 式查询）没有连接池替它设 `options`，所以 DSN
    必须带上——否则它会落在 `public`，看到的是租户 0 的表。
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(_dsn())
    params["options"] = f"-c search_path={schema_for(tenant)}"
    return Settings(database_url=make_conninfo(**params), tenant_id=tenant,
                    base_currency="CNY")


@pytest.fixture()
def two_tenants():
    """建两个临时租户 schema，用完拆掉（含池缓存）。

    用随机后缀：同一个测试库上并发/重跑时，固定名字会互相踩。
    """
    suffix = uuid.uuid4().hex[:8]
    a, b = f"iso_a_{suffix}", f"iso_b_{suffix}"
    sa, sb = _settings_for(a), _settings_for(b)
    for s in (sa, sb):
        create_schema(s)
        migrate(s)
    try:
        yield a, b, sa, sb
    finally:
        for s, t in ((sa, a), (sb, b)):
            close_pool(s)
            try:
                drop_schema(s)
            except Exception:  # noqa: BLE001 - 清理失败不该遮蔽用例的真实结论
                pass
        # 审计行写在 public，按 schema 名清掉本轮的
        try:
            params_dsn = _dsn()
            with psycopg.connect(params_dsn, autocommit=True) as c:
                for t in (a, b):
                    c.execute("DELETE FROM audit WHERE target = %s",
                              (schema_for(t),))
        except Exception:  # noqa: BLE001
            pass


def _insert_entry(settings: Settings, amount: str, txn: str) -> None:
    """在指定租户里写一笔借贷平衡的分录（借 1300 / 贷 4000）。

    列名按真实表结构：`direction`（'debit'/'credit'）+ `amount`，不是 debit/credit 两列。
    """
    with psycopg.connect(settings.database_url, autocommit=True,
                         options=f"-c search_path={schema_for(settings.tenant_id)}") as c:
        c.execute("SELECT set_config('erp.allow_maintenance','off', true)")
        c.execute(
            "INSERT INTO journal_entries (tenant_id, txn_id, line_no, account_code, "
            "direction, amount, amount_base, currency, stable_id, source_ref, booked_on) "
            "VALUES (%s, %s, 1, '1300', 'debit', %s, %s, 'CNY', %s, %s, CURRENT_DATE), "
            "       (%s, %s, 2, '4000', 'credit', %s, %s, 'CNY', %s, %s, CURRENT_DATE)",
            (settings.tenant_id, txn, amount, amount, f"{txn}:1", "probe",
             settings.tenant_id, txn, amount, amount, f"{txn}:2", "probe"),
        )


def test_two_tenants_cannot_see_each_other(two_tenants):
    """4.1：A 写一笔、B 写一笔，各自只看到自己那一笔。"""
    a, b, sa, sb = two_tenants
    txn_a, txn_b = f"iso-{uuid.uuid4().hex[:6]}", f"iso-{uuid.uuid4().hex[:6]}"
    _insert_entry(sa, "10.000000", txn_a)
    _insert_entry(sb, "20.000000", txn_b)

    for s, own, other in ((sa, txn_a, txn_b), (sb, txn_b, txn_a)):
        with psycopg.connect(s.database_url, options=f"-c search_path={schema_for(s.tenant_id)}") as c:
            rows = c.execute("SELECT txn_id FROM journal_entries").fetchall()
        seen = {r[0] for r in rows}
        assert own in seen, f"{s.tenant_id} 看不到自己的分录"
        assert other not in seen, f"{s.tenant_id} 看到了别人的分录（隔离失效）"


def test_missing_where_clause_still_reads_only_own_rows(two_tenants):
    """4.3 的真库形态：那条查询**故意不带** WHERE，仍然只返回本租户的行。

    这是整个 change 的核心主张——隔离不依赖 SQL 写对。
    """
    a, b, sa, sb = two_tenants
    txn_a, txn_b = f"iso-{uuid.uuid4().hex[:6]}", f"iso-{uuid.uuid4().hex[:6]}"
    _insert_entry(sa, "11.000000", txn_a)
    _insert_entry(sb, "22.000000", txn_b)

    with psycopg.connect(sa.database_url, options=f"-c search_path={schema_for(a)}") as c:
        # 无 WHERE、无 tenant 过滤——模拟「代码忘了写条件」
        rows = c.execute("SELECT txn_id FROM journal_entries").fetchall()
    seen = {r[0] for r in rows}
    assert txn_a in seen
    assert txn_b not in seen, "漏写 WHERE 就读到了他租户数据 —— schema 隔离未生效"


def test_triggers_exist_independently_in_each_schema(two_tenants):
    """借贷必平/append-only 触发器必须**每个 schema 各一份**。

    这是 schema 化最容易静默失效的一处：触发器的存在性判断若写成「全库看 tgname」，
    第二个租户建触发器时会因「已存在」被跳过，于是约束在租户 B 上悄悄没了。
    """
    a, b, sa, sb = two_tenants
    for s in (sa, sb):
        schema = schema_for(s.tenant_id)
        with psycopg.connect(_dsn()) as c:
            rows = c.execute(
                "SELECT t.tgname FROM pg_trigger t "
                "JOIN pg_class cl ON cl.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                "WHERE n.nspname = %s AND NOT t.tgisinternal", (schema,)
            ).fetchall()
        names = {r[0] for r in rows}
        assert "journal_entries_balance_deferred" in names, f"{schema} 缺借贷必平触发器"
        assert "journal_entries_append_only" in names, f"{schema} 缺 append-only 触发器"


def test_tenant_drop_leaves_the_other_intact(two_tenants):
    """4.2 的反面：删掉 A 之后，B 的数据与表仍然完整。"""
    a, b, sa, sb = two_tenants
    txn_a, txn_b = f"iso-{uuid.uuid4().hex[:6]}", f"iso-{uuid.uuid4().hex[:6]}"
    _insert_entry(sa, "33.000000", txn_a)
    _insert_entry(sb, "44.000000", txn_b)

    close_pool(sa)
    drop_schema(sa)

    with psycopg.connect(sb.database_url, options=f"-c search_path={schema_for(b)}") as c:
        rows = c.execute("SELECT txn_id FROM journal_entries").fetchall()
    assert {r[0] for r in rows} == {txn_b}

    with psycopg.connect(_dsn()) as c:
        left = c.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema_for(a),)
        ).fetchall()
    assert left == [], "A 的 schema 没被删掉"
