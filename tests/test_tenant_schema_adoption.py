"""迁移期收养：public → tenant_<slug>（openspec: erp-tenant-schema-isolation 6.x）。

**这条测的是 2026-11-18 那次生产事故的根因**：schema 隔离前所有表都在 `public`，
启用后非 `default` 租户的 `search_path` 指向 `tenant_<slug>`，那里是空的 →
启动断言报「数据库未登记任何 schema 版本」→ pod 六次重启、起不来。

正确行为：启动时若「目标 schema 从未建过」且「public 确有历史数据」，把 public 整体
搬进目标 schema（`ALTER TABLE ... SET SCHEMA`，元数据操作、秒级），然后正常启动。

**用独立的第二个库**（`ERP_LEDGER_MIGRATION_TARGET_URL`）：收养的语义就是
「把 public 的表搬走」，在共享测试库里做会**永久清空 public**，而其它用例
（`test_migrations.py` 等）依赖 public 有表 —— 实测一次就把 24 条用例打红。
第二个库专门用来造这个现场，用完即弃。

三条判据（全部要能各自独立成立）：
1. 需要搬时：表与数据都到位，且 `public` 之后是空的
2. 已经搬过：再跑一次什么都不做（幂等）
3. 不该搬时（目标是 public / public 也空）：不动任何东西
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import (adopt_public_into_tenant_schema, close_pool,
                                 create_schema, get_pool, migrate)
from fd_erp_ledger_mcp.tenancy import PUBLIC_SCHEMA, schema_for

pytestmark = pytest.mark.no_db


TARGET_ENV = "ERP_LEDGER_MIGRATION_TARGET_URL"


def _dsn() -> str:
    """**专用第二库**：收养会把 public 的表搬走，不能在共享测试库上做。"""
    dsn = os.environ.get(TARGET_ENV)
    if not dsn:
        pytest.skip(f"{TARGET_ENV} 未设置（收养验收需要一个可清的独立库）")
    return dsn


def _s(tenant: str) -> Settings:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(_dsn())
    params.pop("options", None)  # 收养要能看清 public，别让 search_path 干扰
    return Settings(database_url=make_conninfo(**params), tenant_id=tenant,
                    base_currency="CNY")


def _tables(schema: str) -> set[str]:
    with psycopg.connect(_dsn()) as c:
        return {r[0] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s", (schema,)).fetchall()}


def _clean(public_dsn: str) -> None:
    """把 public 与所有租户 schema 清干净（测试库专用）。"""
    with psycopg.connect(public_dsn, autocommit=True) as c:
        rows = c.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant_up_%'"
        ).fetchall()
        for (n,) in rows:
            c.execute(f'DROP SCHEMA IF EXISTS "{n}" CASCADE')
        # 把 public 里的表搬回去之前先删——这条测试自己负责造 public 现场
        tabs = c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
        for (t,) in tabs:
            c.execute(f'DROP TABLE IF EXISTS "public"."{t}" CASCADE')


@pytest.fixture()
def legacy_public():
    """造出「历史包袱」现场：表在 public（用 default 租户 migrate 得到）。"""
    _clean(_dsn())
    base = _s("default")
    migrate(base)  # default → public，于是 public 有了全套表
    yield base
    _clean(_dsn())
    close_pool(base)


def test_adopts_public_into_a_fresh_tenant_schema(legacy_public):
    """需要搬时：全表搬到目标 schema，public 变空，数据还在。"""
    public_tables = _tables(PUBLIC_SCHEMA)
    assert "journal_entries" in public_tables, "前置现场没造出来"

    tenant = f"up_{uuid.uuid4().hex[:6]}"
    s = _s(tenant)
    try:
        moved = adopt_public_into_tenant_schema(s)
        assert moved is not None, "该搬的时候没搬"
        target = schema_for(tenant)
        assert moved["to"] == target
        assert _tables(target) == public_tables, "表集合没有原样过去"
        assert _tables(PUBLIC_SCHEMA) == set(), "public 应该空掉"

        # 数据也在（版本表能读出来即证明表是活的，不只是空壳）
        conn = get_pool(s).getconn()
        try:
            n = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        finally:
            get_pool(s).putconn(conn)
        assert n > 0
    finally:
        close_pool(s)


def test_second_call_is_a_noop(legacy_public):
    """已经搬过：再跑不做任何事（否则第二次启动会把空 public 再搬一遍、报错或清空目标）。"""
    tenant = f"up_{uuid.uuid4().hex[:6]}"
    s = _s(tenant)
    try:
        first = adopt_public_into_tenant_schema(s)
        assert first is not None
        second = adopt_public_into_tenant_schema(s)
        assert second is None, "第二次调用必须无动作"
        assert _tables(schema_for(tenant))  # 目标还在
    finally:
        close_pool(s)


def test_default_tenant_is_never_adopted(legacy_public):
    """租户 0 就在 public 里，收养对它是 no-op —— 它不该把自己的表搬走。"""
    assert adopt_public_into_tenant_schema(_s("default")) is None
    assert "journal_entries" in _tables(PUBLIC_SCHEMA)


def test_no_public_data_means_no_action():
    """public 也空（全新库）：不搬，交给 migrate() 正常建档。"""
    _clean(_dsn())
    tenant = f"up_{uuid.uuid4().hex[:6]}"
    assert adopt_public_into_tenant_schema(_s(tenant)) is None
