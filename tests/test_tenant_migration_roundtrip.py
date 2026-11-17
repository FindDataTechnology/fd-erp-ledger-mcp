"""共享部署 → 独立实例的搬迁闭环（openspec: erp-tenant-schema-isolation 5.1 / 5.2）。

这是「个人版设计好之后企业版好做」那条产品主张的可执行证据：一个共享租户的数据
导出后，能被一个**单租户实例**原样读出来，不需要任何转换脚本。

用**真的 `pg_dump`**（不是等价模拟）：design D7 承诺的搬迁方式就是
`pg_dump -n <schema> | psql`，测试替它写个近似动作等于没验那条承诺。

两个环境变量：
  ERP_LEDGER_DATABASE_URL        源库（与其它真库用例共用）
  ERP_LEDGER_MIGRATION_TARGET_URL 目标库（空的第二库，搬迁落地处）

`pg_dump` 从 PATH 找；找不到时 skip 并说明——不是失败，因为这是环境缺件。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import close_pool, create_schema, drop_schema, migrate
from fd_erp_ledger_mcp.tenancy import schema_for

pytestmark = pytest.mark.no_db  # 本文件自管库，不走 settings/TRUNCATE 夹具

TARGET_DB_ENV = "ERP_LEDGER_MIGRATION_TARGET_URL"


def _src_dsn() -> str:
    dsn = os.environ.get("ERP_LEDGER_DATABASE_URL")
    if not dsn:
        pytest.skip("ERP_LEDGER_DATABASE_URL 未设置（需真实 Postgres）")
    return dsn


def _dst_dsn() -> str:
    dsn = os.environ.get(TARGET_DB_ENV)
    if not dsn:
        pytest.skip(f"{TARGET_DB_ENV} 未设置（搬迁验收需要一个空的第二库）")
    return dsn


def _settings(dsn: str, tenant: str) -> Settings:
    params = conninfo_to_dict(dsn)
    params["options"] = f"-c search_path={schema_for(tenant)}"
    return Settings(database_url=make_conninfo(**params), tenant_id=tenant,
                    base_currency="CNY")


def _insert(settings: Settings, txn: str) -> None:
    """写一笔借贷平衡的分录（借 1300 / 贷 4000，各 7.5）。"""
    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute("SELECT set_config('erp.allow_maintenance','off', true)")
        c.execute(
            "INSERT INTO journal_entries (tenant_id, txn_id, line_no, account_code, "
            "direction, amount, amount_base, currency, stable_id, source_ref, booked_on) "
            "VALUES (%s,%s,1,'1300','debit',%s,%s,'CNY',%s,'probe',CURRENT_DATE),"
            "       (%s,%s,2,'4000','credit',%s,%s,'CNY',%s,'probe',CURRENT_DATE)",
            (settings.tenant_id, txn, "7.500000", "7.500000", f"{txn}:1",
             settings.tenant_id, txn, "7.500000", "7.500000", f"{txn}:2"),
        )


def _tables(dsn: str, schema: str) -> set[str]:
    with psycopg.connect(dsn) as c:
        return {r[0] for r in c.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE'", (schema,)).fetchall()}


def _pg_dump_schema(dsn: str, schema: str) -> str:
    """`pg_dump -n <schema> --no-owner --no-privileges`，把 SQL 交给调用方。

    `--no-owner/--no-privileges` 是必需的：客户机房的角色名与共享部署不同，
    带着 owner 语句导入会直接失败。
    """
    exe = shutil.which("pg_dump") or "/opt/homebrew/opt/postgresql@17/bin/pg_dump"
    if not shutil.which("pg_dump") and not os.path.exists(exe):
        pytest.skip("pg_dump 不在 PATH（搬迁验收需要它）")
    params = conninfo_to_dict(dsn)
    args = [exe, "--no-owner", "--no-privileges", "-n", schema]
    for key in ("host", "port", "user", "dbname"):
        if params.get(key):
            args += [f"--{key}", str(params[key])]
    env = dict(os.environ)
    if params.get("password"):
        env["PGPASSWORD"] = str(params["password"])
    out = subprocess.run(args, capture_output=True, text=True, env=env,
                         check=False, timeout=120)
    if out.returncode != 0:
        pytest.fail(f"pg_dump 失败：{out.stderr.strip()[:400]}")
    return out.stdout


def _psql_apply(dsn: str, sql: str) -> None:
    """把 dump 的 SQL 灌进目标库（psql -v ON_ERROR_STOP=1）。"""
    exe = shutil.which("psql") or "/opt/homebrew/opt/postgresql@17/bin/psql"
    params = conninfo_to_dict(dsn)
    args = [exe, "-v", "ON_ERROR_STOP=1", "-q"]
    for key in ("host", "port", "user", "dbname"):
        if params.get(key):
            args += [f"--{key}", str(params[key])]
    env = dict(os.environ)
    if params.get("password"):
        env["PGPASSWORD"] = str(params["password"])
    out = subprocess.run(args, input=sql, capture_output=True, text=True,
                         env=env, check=False, timeout=120)
    if out.returncode != 0:
        pytest.fail(f"psql 导入失败：{out.stderr.strip()[:400]}")


@pytest.fixture()
def moved():
    src, dst = _src_dsn(), _dst_dsn()
    tenant = f"move_{uuid.uuid4().hex[:8]}"
    schema = schema_for(tenant)
    s = _settings(src, tenant)
    create_schema(s)
    migrate(s)
    txn = f"mv-{uuid.uuid4().hex[:6]}"
    _insert(s, txn)
    # 搬到目标库：真 pg_dump -n | psql。目标库用**同名 schema**（design D7：
    # 改名会逼出一份转换脚本，而那正是本方案要避免的东西）。
    sql = _pg_dump_schema(src, schema)
    _psql_apply(dst, sql)
    try:
        yield src, dst, tenant, schema, txn
    finally:
        close_pool(s)
        for d, t in ((src, tenant),):
            try:
                drop_schema(_settings(d, t))
            except Exception:  # noqa: BLE001
                pass
        try:
            with psycopg.connect(dst, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        except Exception:  # noqa: BLE001
            pass


def test_dump_carries_the_isomorphic_table_set(moved):
    """5.1：搬过去的表集合与源库逐表相同（同构＝搬迁无需转换）。"""
    src, dst, _tenant, schema, _txn = moved
    assert _tables(src, schema) == _tables(dst, schema)


def test_rows_survive_the_move_unchanged(moved):
    """5.1：分录与金额逐项相同——搬迁不变形。"""
    _src, dst, _tenant, schema, txn = moved
    with psycopg.connect(dst) as c:
        rows = c.execute(
            f'SELECT txn_id, amount FROM "{schema}".journal_entries').fetchall()
    assert {r[0] for r in rows} == {txn}
    assert {str(r[1]) for r in rows} == {"7.500000"}


def test_constraints_arrive_with_the_dump(moved):
    """5.1：借贷必平的约束/触发器跟着 dump 一起到——否则独立实例上账不受约束。"""
    _src, dst, _tenant, schema, _txn = moved
    with psycopg.connect(dst) as c:
        trig = {r[0] for r in c.execute(
            "SELECT t.tgname FROM pg_trigger t "
            "JOIN pg_class cl ON cl.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=cl.relnamespace "
            "WHERE n.nspname=%s AND NOT t.tgisinternal", (schema,)).fetchall()}
    assert "journal_entries_balance_deferred" in trig, "搬迁后借贷必平约束丢失"
    assert "journal_entries_append_only" in trig, "搬迁后 append-only 约束丢失"


def test_target_instance_serves_the_same_posture(moved):
    """5.2：目标库以**单租户形态**跑同一份代码，查得出搬过去的账。"""
    _src, dst, tenant, _schema, txn = moved
    target = _settings(dst, tenant)  # 同一份代码、只换 DSN
    try:
        with psycopg.connect(target.database_url) as c:
            got = c.execute("SELECT txn_id FROM journal_entries").fetchall()
        assert {r[0] for r in got} == {txn}
    finally:
        close_pool(target)
