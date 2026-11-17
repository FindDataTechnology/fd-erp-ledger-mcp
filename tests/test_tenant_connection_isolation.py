"""连接池不串租户（openspec: erp-tenant-schema-isolation 1.3）。

真库用例：`search_path` 是会话级状态，psycopg 的池会复用连接，所以「借出两次
是不是同一个 schema」和「两个租户会不会拿到同一个池」都必须实测，不能靠读代码。

跨进程那半边用子进程跑同一份代码：池的缓存键在进程内，跨进程要证的是
`ERP_TENANT_ID` 真的决定了 `search_path`（而不是被某个模块级默认覆盖）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import close_pool, connection, create_schema, migrate
from fd_erp_ledger_mcp.tenancy import schema_for


def _base_dsn(dsn: str) -> dict:
    """去掉夹具注入的 `options`，拿回裸 DSN 参数（要自己指定租户时用）。"""
    params = conninfo_to_dict(dsn)
    params.pop("options", None)
    return params


def _settings_for(settings: Settings, tenant_id: str) -> Settings:
    return Settings(database_url=make_conninfo(**_base_dsn(settings.database_url)),
                    tenant_id=tenant_id, base_currency="CNY")


def _search_path(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        return cur.fetchone()[0]


def test_two_borrows_from_one_pool_agree(settings):
    """同一池借出两次，`search_path` 必须一致——池复用不得让第二次继承别人的会话。"""
    paths = []
    for _ in range(2):
        with connection(settings) as conn:
            paths.append(_search_path(conn))
    assert paths[0] == paths[1]
    assert schema_for(settings.tenant_id) in paths[0]


def test_two_tenants_in_one_process_never_share_search_path(settings):
    """同进程两个租户 ⇒ 各自指向自己的 schema（池的键带 schema 的直接判据）。"""
    a = _settings_for(settings, "tenant_a")
    b = _settings_for(settings, "tenant_b")
    create_schema(a)
    create_schema(b)
    try:
        with connection(a) as conn:
            assert schema_for("tenant_a") in _search_path(conn)
        with connection(b) as conn:
            assert schema_for("tenant_b") in _search_path(conn)
        # 交错再借一次：后借的不能污染先借的
        with connection(b) as conn_b, connection(a) as conn_a:
            assert schema_for("tenant_a") in _search_path(conn_a)
            assert schema_for("tenant_b") in _search_path(conn_b)
    finally:
        close_pool(a)
        close_pool(b)


def test_search_path_survives_a_transaction_block(settings):
    """事务块结束不改 `search_path`：它是会话级设定，不是事务级 `SET LOCAL`。"""
    with connection(settings) as conn:
        before = _search_path(conn)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        assert _search_path(conn) == before


@pytest.mark.parametrize("tenant", ["tenant_c", "tenant_d"])
def test_cross_process_tenant_decides_schema(settings, tenant):
    """跨进程：子进程带 `ERP_TENANT_ID` 启动，`search_path` 必须由它决定。"""
    base = _base_dsn(settings.database_url)
    dsn = make_conninfo(**base)
    code = textwrap.dedent(
        """
        import json
        from fd_erp_ledger_mcp.config import Settings
        from fd_erp_ledger_mcp.db import create_schema, connection
        from fd_erp_ledger_mcp.tenancy import schema_for

        s = Settings.from_env()
        create_schema(s)
        with connection(s) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                print(json.dumps({"tenant": s.tenant_id, "path": cur.fetchone()[0],
                                  "expected": schema_for(s.tenant_id)}))
        """
    )
    env = dict(os.environ, ERP_LEDGER_DATABASE_URL=dsn, ERP_TENANT_ID=tenant)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    import json
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["tenant"] == tenant
    assert payload["expected"] in payload["path"]
