"""连接池的 `search_path` 与缓存键（openspec: erp-tenant-schema-isolation 1.2）。

离线用例：把 `psycopg_pool.ConnectionPool` 换成一个只记录参数的假类，所以不连库、
也不受 `ERP_LEDGER_DATABASE_URL` 是否设置影响（不标 `no_db` 会被真库夹具整体 skip）。
"""
from __future__ import annotations

import pytest
import psycopg_pool

from fd_erp_ledger_mcp import db
from fd_erp_ledger_mcp.config import Settings

pytestmark = pytest.mark.no_db


class _FakePool:
    """记录 dsn 与 kwargs 的替身；不发连接。"""

    instances: list["_FakePool"] = []

    def __init__(self, dsn: str, **kwargs):
        self.dsn = dsn
        self.kwargs = kwargs
        _FakePool.instances.append(self)


@pytest.fixture(autouse=True)
def _fake_pool(monkeypatch):
    _FakePool.instances.clear()
    db._pools.clear()
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakePool)
    yield
    db._pools.clear()
    _FakePool.instances.clear()


def _settings(tenant_id: str) -> Settings:
    return Settings(database_url="postgresql://u@h:5432/db", tenant_id=tenant_id)


def test_default_tenant_pool_targets_public():
    """`ERP_TENANT_ID` 未设 ⇒ search_path 指向 public（生产零变更的直接原因）。"""
    pool = db.get_pool(_settings("default"))
    assert pool.kwargs["kwargs"]["options"] == "-c search_path=public"


def test_tenant_pool_targets_its_schema():
    """自定义租户 ⇒ search_path 指向 `tenant_<slug>`。"""
    pool = db.get_pool(_settings("acme"))
    assert pool.kwargs["kwargs"]["options"] == "-c search_path=tenant_acme"


def test_autocommit_survives_the_options_injection():
    """`options` 是加在既有 kwargs 上的，不能把 autocommit 挤掉。

    DEFERRABLE 借贷必平约束只在真实顶层事务边界触发（见 get_pool 注释),
    丢了 autocommit 会让不平分录在落账时静默通过。
    """
    pool = db.get_pool(_settings("acme"))
    assert pool.kwargs["kwargs"]["autocommit"] is True


def test_two_tenants_never_share_a_pool():
    """同 dsn、不同租户 ⇒ 必须是两个池；共用就是 `search_path` 串租户。

    这条是本 change 里唯一「不报错的坏结果」的守门判据：池按 dsn 缓存时，
    第二个租户拿到第一个租户的池，查询照样成功、数据却是别人家的。
    """
    a = db.get_pool(_settings("acme"))
    b = db.get_pool(_settings("beta"))
    assert a is not b
    assert a.kwargs["kwargs"]["options"] == "-c search_path=tenant_acme"
    assert b.kwargs["kwargs"]["options"] == "-c search_path=tenant_beta"
    assert len(_FakePool.instances) == 2


def test_same_tenant_reuses_its_pool():
    """同租户同 dsn ⇒ 复用同一个池（否则每个工具调用都新建 8 条连接的池）。"""
    first = db.get_pool(_settings("acme"))
    second = db.get_pool(_settings("acme"))
    assert first is second
    assert len(_FakePool.instances) == 1


def test_illegal_tenant_is_rejected_before_a_pool_is_built():
    """非法租户在池构造前就被白名单挡住——不能等到 SET search_path 才发现。"""
    from fd_erp_ledger_mcp.errors import ErpError

    with pytest.raises(ErpError) as ei:
        db.get_pool(_settings("客户甲"))
    assert ei.value.code == "INVALID_ARGUMENT"
    assert _FakePool.instances == []
