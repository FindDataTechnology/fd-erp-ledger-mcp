"""租户 schema 运维命令的保护逻辑（openspec: erp-tenant-schema-isolation 2.3/2.4）。

这里只测**不连库就能判定的部分**：租户 0 的保护、确认门、schema 名派生。
真正建/删 schema 需要真 PG，在 `test_tenant_lifecycle_db.py` 里（标 db 用例）。
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from fd_erp_ledger_mcp import cli as cli_mod

pytestmark = pytest.mark.no_db


@pytest.fixture()
def runner():
    return CliRunner()


def test_drop_public_is_refused_before_touching_the_database(runner):
    """`default` 是租户 0（public schema），无论有没有 --yes 都必须硬拒。

    这条是安全底线：误删 public = 抹掉生产库。CLI 层拦一道，`db.drop_schema`
    再拦一道（见下一个用例）。
    """
    for extra in ([], ["--yes"]):
        r = runner.invoke(cli_mod.cli, ["tenant", "drop", "default", *extra])
        assert r.exit_code != 0, f"default 必须被拒（args={extra}）"
        assert "TENANT_ZERO_PROTECTED" in r.output


def test_drop_without_yes_is_refused(runner):
    """不可逆操作缺省只做拒绝，不执行——确认门是 `--yes`，不是「跑一次看看」。"""
    r = runner.invoke(cli_mod.cli, ["tenant", "drop", "acme"])
    assert r.exit_code != 0
    assert "CONFIRMATION_REQUIRED" in r.output


def test_drop_refuses_illegal_tenant_id(runner):
    """非法租户 id 在到达 DDL 之前就被 `schema_for` 拒绝（白名单是唯一入口）。

    `ErpError` 直接冒出（click 不捕获），所以断言的是 `result.exception` 而不是
    `output`——这一条同时也钉住「白名单在 DDL 之前跑」，否则非法 schema 名会先被
    拼进 `DROP SCHEMA`。
    """
    from fd_erp_ledger_mcp.errors import ErpError

    r = runner.invoke(cli_mod.cli, ["tenant", "drop", "中文租户", "--yes"])
    assert r.exit_code != 0
    assert isinstance(r.exception, ErpError)
    assert r.exception.code == "INVALID_ARGUMENT"


def test_create_default_is_a_noop(runner):
    """开通 default 不改任何东西：public 是库内置的，零 DD​L 是短路保证。"""
    r = runner.invoke(cli_mod.cli, ["tenant", "create", "default"])
    assert r.exit_code == 0
    assert '"schema": "public"' in r.output
    assert "无需开通" in r.output
