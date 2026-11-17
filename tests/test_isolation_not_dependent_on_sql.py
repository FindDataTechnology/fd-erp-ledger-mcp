"""隔离不依赖查询条件（openspec: erp-tenant-schema-isolation 4.3 的离线形态）。

本 change 的核心主张是：**漏写 `WHERE tenant_id=?` 也读不到别人的数据**，因为边界
在 `search_path` 而不在 SQL 里。这条主张不能靠「读一遍代码觉得对」来交付，必须有一
条会红的测试。

真库版本（`test_tenant_data_isolation.py`）需要双 schema + 双连接，受 DSN 门控；
这里用假连接把**机制**钉住：每种租户配置下，连接池交给业务代码的那条连接，
其 `options` 必须已经把 `search_path` 定向到该租户的 schema。业务代码随后发什么
SQL 都不影响这一点——它根本没有能力改连接的 `search_path`。
"""
from __future__ import annotations

import pytest
import psycopg_pool

from fd_erp_ledger_mcp import db
from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.tenancy import PUBLIC_SCHEMA, schema_for

pytestmark = pytest.mark.no_db


class _FakePool:
    instances: list["_FakePool"] = []

    def __init__(self, dsn: str, **kwargs):
        self.dsn = dsn
        self.kwargs = kwargs
        _FakePool.instances.append(self)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _FakePool.instances.clear()
    db._pools.clear()
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakePool)
    yield
    db._pools.clear()
    _FakePool.instances.clear()


def _s(tenant: str) -> Settings:
    return Settings(database_url="postgresql://u@h:5432/db", tenant_id=tenant)


def test_every_connection_is_born_inside_its_own_schema():
    """连接一出生就定向，业务 SQL 无权改它——这是「漏 WHERE 也不串」的机制本身。"""
    for tenant in ("default", "acme", "factory_a"):
        pool = db.get_pool(_s(tenant))
        expected = schema_for(tenant)
        assert pool.kwargs["kwargs"]["options"] == f"-c search_path={expected}", tenant


def test_two_tenants_get_two_pools_never_one():
    """同一进程内两个租户绝不共用一个池——共池就是共 `search_path`，那是静默串租户。"""
    p1 = db.get_pool(_s("acme"))
    p2 = db.get_pool(_s("factory_a"))
    assert p1 is not p2
    assert p1.kwargs["kwargs"]["options"] != p2.kwargs["kwargs"]["options"]


def test_business_layer_cannot_re_aim_the_connection():
    """`connection()` 只是把池里的连接交出去，中间没有任何「按请求改 search_path」的口子。

    如果将来有人在这里加一句 `SET search_path`，这条测试仍会绿（它不追踪运行时 SQL），
    但 `test_pool_search_path.py` 的缓存键断言会红——两处合起来堵住「运行期改指向」。
    """
    settings = _s("acme")
    pool = db.get_pool(settings)
    assert "search_path=tenant_acme" in pool.kwargs["kwargs"]["options"]
    # 业务层拿到的 Settings 里租户是 acme，但**隔离不靠它**——靠池的 options。
    assert settings.tenant_id == "acme"


def test_tenant_zero_maps_to_public_so_production_is_unchanged():
    """生产零变更的机制：`default` → `public`，而 `public` 是库默认 search_path。"""
    pool = db.get_pool(_s("default"))
    assert pool.kwargs["kwargs"]["options"] == f"-c search_path={PUBLIC_SCHEMA}"
