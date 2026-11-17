"""`migrate()` 在目标 schema 内重放（openspec: erp-tenant-schema-isolation 2.1）。

三条判据：
1. `public` 重跑是 no-op、版本不变（现状行为逐字节不变）；
2. 新租户 schema 里建出**同构**的一组表（与 `public` 同集合，不硬编码张数）；
3. 在别的 schema 上重放**不触碰** `public` 的 `schema_migrations`（每 schema 一份）。
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import (REQUIRED_SCHEMA_VERSION, close_pool, create_schema,
                                get_pool, migrate)
from fd_erp_ledger_mcp.tenancy import PUBLIC_SCHEMA, schema_for


def _base_dsn(dsn: str) -> dict:
    params = conninfo_to_dict(dsn)
    params.pop("options", None)
    return params


def _settings_for(settings: Settings, tenant_id: str) -> Settings:
    return Settings(database_url=make_conninfo(**_base_dsn(settings.database_url)),
                    tenant_id=tenant_id, base_currency="CNY")


@pytest.fixture(scope="module", autouse=True)
def _public_is_migrated(settings):
    """本模块的判据都以「`public` 已建过」为前提，所以先把它建出来。

    为什么需要显式这一步：`conftest` 的 `settings` 夹具只 migrate **本租户**
    （`tenant_acme`），从不碰 `public`。而测试库的 `public` 通常是空的——于是
    「public 重跑是 no-op」「租户 schema 与 public 同构」「重放不触碰 public 版本」
    这三条都以「public 不存在」失败，红的原因与被测行为无关。

    这也是产品侧的一条真实约定：**租户 0 不是自动建好的**，生产里它是历史包袱
    （库一直就是 public），新建库里它需要一次显式 migrate。
    """
    public = _settings_for(settings, "default")
    migrate(public)
    yield
    close_pool(public)



def _drop(settings: Settings) -> None:
    """删掉本用例建的租户 schema（幂等）。

    只 `close_pool` 会把 schema 连同它的表留在测试库里；下一次跑同一用例就带着
    上一次的残留状态（`mig_c` 实测残留过一次，导致 replay 断言假红——「迁移没重放」
    其实是「上一轮的版本表还在」）。
    """
    schema = schema_for(settings.tenant_id)
    if schema == PUBLIC_SCHEMA:
        return
    try:
        with _conn(settings) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    except Exception:  # noqa: BLE001 - 清理失败不遮蔽用例结论
        pass


def _conn(settings: Settings):
    return psycopg.connect(settings.database_url, autocommit=True)


def _tables(settings: Settings, schema: str) -> set[str]:
    with _conn(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            )
            return {r[0] for r in cur.fetchall()}


def _versions(settings: Settings, schema: str) -> set[str]:
    with _conn(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT version FROM "{schema}".schema_migrations')
            return {r[0] for r in cur.fetchall()}


def test_public_replay_is_a_noop(settings):
    """`public` 上重跑 `migrate()` ⇒ applied 为空，版本不动。"""
    public = _settings_for(settings, "default")
    before = _versions(public, PUBLIC_SCHEMA)
    result = migrate(public)
    assert result["applied"] == []
    assert result["required"] == REQUIRED_SCHEMA_VERSION
    assert _versions(public, PUBLIC_SCHEMA) == before


def test_tenant_schema_gets_an_isomorphic_set_of_tables(settings):
    """新租户 schema 建出与 public **同集合**的表（同构＝搬迁无需转换脚本）。"""
    tenant = _settings_for(settings, "mig_a")
    create_schema(tenant)
    try:
        result = migrate(tenant)
        assert result["applied"], "新 schema 必须真的重放迁移，不能空跑"
        tenant_tables = _tables(tenant, schema_for("mig_a"))
        public_tables = _tables(tenant, PUBLIC_SCHEMA)
        assert tenant_tables == public_tables
        assert "schema_migrations" in tenant_tables
        # 同构的另一半：重跑幂等
        assert migrate(tenant)["applied"] == []
    finally:
        close_pool(tenant)
        _drop(tenant)


def test_replaying_one_schema_does_not_touch_public_versions(settings):
    """在 tenant schema 上重放不得改动 `public.schema_migrations`。"""
    public = _settings_for(settings, "default")
    before = _versions(public, PUBLIC_SCHEMA)
    tenant = _settings_for(settings, "mig_b")
    create_schema(tenant)
    try:
        migrate(tenant)
        assert _versions(public, PUBLIC_SCHEMA) == before
        got = _versions(tenant, schema_for("mig_b"))
        assert got == before, "两个 schema 的版本集合应各自独立且此时相同（同一份代码）"
    finally:
        close_pool(tenant)
        _drop(tenant)


def test_schema_migrations_is_per_schema(settings):
    """每 schema 一份 `schema_migrations`：删掉租户那份，public 的仍在。"""
    tenant = _settings_for(settings, "mig_c")
    create_schema(tenant)
    schema = schema_for("mig_c")
    try:
        migrate(tenant)
        with _conn(tenant) as conn:
            conn.execute(f'DROP TABLE "{schema}".schema_migrations')
            conn.execute(f'DROP TABLE "{schema}".accounts')
        assert _tables(tenant, PUBLIC_SCHEMA) >= {"schema_migrations", "accounts"}
        # 重建：migrate 会重新建表并把版本补齐
        assert migrate(tenant)["applied"]
        assert _versions(tenant, schema) == _versions(tenant, PUBLIC_SCHEMA)
    finally:
        close_pool(tenant)
        _drop(tenant)


def test_pool_of_a_schema_is_what_migrate_writes_into(settings):
    """`migrate()` 写哪儿由**池的 search_path** 决定，不由调用方传参决定。"""
    tenant = _settings_for(settings, "mig_d")
    create_schema(tenant)
    try:
        migrate(tenant)
        with get_pool(tenant).connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_schema()")
                assert cur.fetchone()[0] == schema_for("mig_d")
    finally:
        close_pool(tenant)
        _drop(tenant)
