"""`tenant list --check-versions` 的判据（openspec: erp-tenant-schema-isolation 3.2）。

判据本身要能**双向**成立：净态 exit 0、脏态 exit 1 并点名到具体 schema。
只验一边的判据与没有判据长得一样——2026-11-17 的 `list_posting_rules` 就是
「闸全绿但工具恒失败」的同族教训。

真库用例（需 DSN）；离线路径（非法租户、default 短路）在
`test_tenant_cli_guards.py`。
"""
from __future__ import annotations

import os
import uuid

import pytest
from click.testing import CliRunner

from fd_erp_ledger_mcp import cli as cli_mod
from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import close_pool, create_schema, drop_schema, migrate
from fd_erp_ledger_mcp.tenancy import schema_for


def _require_dsn() -> str:
    dsn = os.environ.get("ERP_LEDGER_DATABASE_URL")
    if not dsn:
        pytest.skip("ERP_LEDGER_DATABASE_URL 未设置（需真实 Postgres）")
    return dsn


@pytest.fixture()
def clean_tenant():
    """建一个迁移完整的租户，用后删。"""
    _require_dsn()
    tid = f"ver_{uuid.uuid4().hex[:8]}"
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(_require_dsn())
    params["options"] = f"-c search_path={schema_for(tid)}"
    s = Settings(database_url=make_conninfo(**params), tenant_id=tid,
                 base_currency="CNY")
    create_schema(s)
    migrate(s)
    try:
        yield tid, s
    finally:
        close_pool(s)
        try:
            drop_schema(s)
        except Exception:  # noqa: BLE001
            pass


def test_clean_tenant_passes(clean_tenant, monkeypatch):
    """净态：迁移完整的租户 ⇒ **它自己**判 ok。

    不断言全局 `consistent`：测试库是共享的，别的用例可能留下空 schema（被判
    `unreadable`）。拿全局结论当本租户结论，会让这条用例按执行顺序时绿时红——
    那正是 11-13 那次「只有一行」假红同型的顺序依赖。
    """
    tid, _s = clean_tenant
    monkeypatch.setenv("ERP_LEDGER_DATABASE_URL", _require_dsn())
    r = CliRunner().invoke(cli_mod.cli, ["tenant", "list", "--check-versions"])
    import json

    body = json.loads(r.output)
    mine = body["versions"]["tenants"].get(tid)
    assert mine is not None, f"判据没报到本租户 {tid}"
    assert mine["status"] == "ok", mine
    assert mine["applied"] == mine["required"]


def test_tenant_missing_a_migration_is_named(clean_tenant, monkeypatch):
    """脏态：删掉一条版本记录 ⇒ consistent=false、退出非零、**点名**到该租户。"""
    tid, s = clean_tenant
    schema = schema_for(tid)
    import psycopg

    with psycopg.connect(s.database_url, autocommit=True) as c:
        c.execute(f'DELETE FROM "{schema}".schema_migrations WHERE version = '
                  "(SELECT min(version) FROM schema_migrations)")
    monkeypatch.setenv("ERP_LEDGER_DATABASE_URL", _require_dsn())
    r = CliRunner().invoke(cli_mod.cli, ["tenant", "list", "--check-versions"])
    assert r.exit_code != 0
    assert '"consistent": false' in r.output
    assert f'"{tid}"' in r.output, "判据必须点名是哪个租户漂移了"
    assert "missing" in r.output, "必须说明缺哪些版本，而不是只说不一致"


def test_unreadable_tenant_is_not_a_pass(clean_tenant, monkeypatch):
    """读不到版本表 ≠ 通过：空 schema（没 migrate 过）必须判不一致。

    这条是「静默放行」的最短路径——如果读失败被当成通过，一个从没建过表的租户
    会永远显示健康。
    """
    _require_dsn()
    tid = f"empty_{uuid.uuid4().hex[:8]}"
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from fd_erp_ledger_mcp.db import get_pool

    params = conninfo_to_dict(_require_dsn())
    params["options"] = f"-c search_path={schema_for(tid)}"
    s = Settings(database_url=make_conninfo(**params), tenant_id=tid, base_currency="CNY")
    # 只建 schema、不 migrate。注意：如果库里 `public` 有历史表，启动期的收养逻辑
    # （adopt_public_into_tenant_schema）会把它们搬进来，这条用例就不再是「空 schema」。
    # 所以显式把 public 的表挡住——本用例要验的是 `unreadable`，不是收养。
    create_schema(s)  # 只建 schema、不 migrate
    try:
        monkeypatch.setenv("ERP_LEDGER_DATABASE_URL", _require_dsn())
        r = CliRunner().invoke(cli_mod.cli, ["tenant", "list", "--check-versions"])
        assert r.exit_code != 0
        assert "unreadable" in r.output
    finally:
        close_pool(s)
        try:
            drop_schema(s)
        except Exception:  # noqa: BLE001
            pass
