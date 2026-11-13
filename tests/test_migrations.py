"""迁移阶段（1.3 db-schema-lifecycle）：一次性、可重放、版本闸、adopt。"""
from __future__ import annotations

import psycopg
import pytest

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.db import (MIGRATIONS_DIR, REQUIRED_SCHEMA_VERSION,
                                  assert_schema_version, mark_adopted, migrate)
from fd_erp_ledger_mcp.errors import ErpError


def test_migrate_replay_is_noop(settings):
    """re-apply 是 no-op：不重跑任何 DDL，登记版本不变。"""
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        before = conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version").fetchall()
    finally:
        conn.close()

    result = migrate(settings)
    assert result["applied"] == []  # 已登记 → 本次零应用

    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        after = conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version").fetchall()
    finally:
        conn.close()
    assert before == after  # 连 applied_at 都未动


def test_schema_version_gate_passes(settings):
    assert_schema_version(settings)  # 不抛即通过


def test_schema_version_gate_fails_fast_on_missing(settings):
    """未登记版本的库必须 fail-fast（naming required + actual）。"""
    # 同服务器起一个无 schema_migrations 的临时库，避免硬编码环境端口。
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    base = conninfo_to_dict(settings.database_url)
    scratch = dict(base, dbname="erp_gate_scratch")
    scratch_dsn = make_conninfo(**scratch)
    admin = psycopg.connect(settings.database_url, autocommit=True)
    try:
        try:
            admin.execute("DROP DATABASE IF EXISTS erp_gate_scratch")
            admin.execute("CREATE DATABASE erp_gate_scratch")
        except psycopg.errors.InsufficientPrivilege:
            # 测试账号无 CREATEDB（如共享实例的 app 角色）：无法建 scratch 库，
            # 该场景只能在有权限环境验证，这里如实跳过而非伪装通过。
            pytest.skip("测试账号无 CREATEDB 权限，无法创建 scratch 库验证 fail-fast 闸")
    finally:
        admin.close()
    s = Settings(database_url=scratch_dsn, tenant_id="x")
    try:
        c = psycopg.connect(s.database_url, autocommit=True)
        try:
            c.execute("DROP TABLE IF EXISTS schema_migrations")
        finally:
            c.close()
        with pytest.raises(ErpError) as ei:
            assert_schema_version(s)
        assert ei.value.code == "SCHEMA_OUTDATED"
        assert ei.value.detail["required"] == REQUIRED_SCHEMA_VERSION
    finally:
        from fd_erp_ledger_mcp.db import _pools  # 关闭临时库连接池再 DROP
        pool = _pools.pop(scratch_dsn, None)
        if pool is not None:
            pool.close()
        admin = psycopg.connect(settings.database_url, autocommit=True)
        try:
            admin.execute("DROP DATABASE IF EXISTS erp_gate_scratch")
        finally:
            admin.close()


def test_mark_adopted_executes_no_ddl(settings):
    """adopt 只登记版本，不执行数据定义语句。"""
    result = mark_adopted(settings, "0001_baseline")
    assert result["ddl_executed"] is False
    assert_schema_version(settings)


def test_universal_accounts_preset_seeded(settings):
    """0002：通用科目最小集预设已种入（五类别各最小组）。"""
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        rows = conn.execute(
            "SELECT code, name, name_en, account_type FROM universal_accounts "
            "ORDER BY code").fetchall()
    finally:
        conn.close()
    by_type = {}
    for _code, _name, _name_en, atype in rows:
        by_type.setdefault(atype, []).append(_code)
    assert set(by_type) == {"asset", "liability", "equity", "revenue", "expense"}
    assert len(rows) == 18  # 8 资产（含 1602 累计折旧、1231 坏账准备两项抵减、1401 生产成本） + 2 负债 + 2 权益 + 1 收入 + 5 费用
    assert by_type["revenue"] == ["4000"]  # 收入唯一内核科目，行业细目走映射


def test_seed_universal_accounts_idempotent(settings):
    """逐租户种子幂等：重复调用零新增，科目类型与预设一致。"""
    from fd_erp_ledger_mcp.universal_accounts import seed_universal_accounts
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        first = seed_universal_accounts(conn, "seed-t1")
        assert first["seeded"] == 18
        again = seed_universal_accounts(conn, "seed-t1")
        assert again["seeded"] == 0  # 幂等：零新增
        assert again["universal_present"] == 18
        types = dict(conn.execute(
            "SELECT code, account_type FROM accounts WHERE tenant_id='seed-t1' "
            "AND code IN (SELECT code FROM universal_accounts)").fetchall())
        assert types["4000"] == "revenue" and types["1000"] == "asset"
        assert types["5100"] == "expense" and types["2100"] == "liability"
    finally:
        conn.close()


def test_fixed_assets_accounts_present(settings):
    """0012：1601 固定资产 / 1602 累计折旧在预设里，且重放 DDL 幂等。"""
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        rows = dict(
            (r[0], r[1:]) for r in conn.execute(
                "SELECT code, account_type, note FROM universal_accounts "
                "WHERE code IN ('1601', '1602')").fetchall())
        assert set(rows) == {"1601", "1602"}, f"缺科目：{sorted(rows)}"
        # 1602 是抵减资产：封闭 account_type 无 contra-asset，按 asset 登记
        assert rows["1601"][0] == "asset"
        assert rows["1602"][0] == "asset"
        assert "抵减" in (rows["1602"][1] or ""), rows["1602"][1]

        # 重放两遍证明 DDL 本身幂等（migrate 会跳过已登记版本，故直接重放 SQL）
        sql = (MIGRATIONS_DIR / "0012_fixed_assets_accounts.sql").read_text(encoding="utf-8")
        conn.execute(sql)
        conn.execute(sql)
        total = conn.execute("SELECT COUNT(*) FROM universal_accounts").fetchone()[0]
        assert total == 18
    finally:
        conn.close()


def test_0015_bad_debt_accounts_in_preset(settings):
    """0015：1231 坏账准备 / 6602 信用减值损失在预设里（报表净额与费用口径依赖它们）。"""
    from fd_erp_ledger_mcp.universal_accounts import seed_universal_accounts
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        seed_universal_accounts(conn, "seed-0015")
        rows = conn.execute(
            "SELECT code, account_type FROM universal_accounts"
            " WHERE code IN ('1231', '6602') ORDER BY code").fetchall()
    finally:
        conn.close()
    assert [(c, t) for c, t in rows] == [("1231", "asset"), ("6602", "expense")]


def test_0016_fx_account_in_preset(settings):
    """0016：6603 汇兑损益在预设里（期末调汇的入账科目，利润表单列一行）。"""
    from fd_erp_ledger_mcp.universal_accounts import seed_universal_accounts
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        seed_universal_accounts(conn, "seed-0016")
        rows = conn.execute(
            "SELECT code, account_type, note FROM universal_accounts"
            " WHERE code = '6603'").fetchall()
        # 重放两遍证明追加式幂等
        sql = (MIGRATIONS_DIR / "0016_fx_accounts.sql").read_text(encoding="utf-8")
        conn.execute(sql)
        conn.execute(sql)
        total = conn.execute(
            "SELECT COUNT(*) FROM universal_accounts WHERE code = '6603'").fetchone()[0]
    finally:
        conn.close()
    assert [(c, t) for c, t, _ in rows] == [("6603", "expense")]
    assert "期末汇率" in (rows[0][2] or "")
    assert total == 1


def test_0017_wip_account_in_preset(settings):
    """0017：1401 生产成本在预设里（在制品；领料借记、完工贷记）。"""
    from fd_erp_ledger_mcp.universal_accounts import seed_universal_accounts
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        seed_universal_accounts(conn, "seed-0017")
        rows = conn.execute(
            "SELECT code, account_type, note FROM universal_accounts"
            " WHERE code = '1401'").fetchall()
        sql = (MIGRATIONS_DIR / "0017_wip_account.sql").read_text(encoding="utf-8")
        conn.execute(sql)
        conn.execute(sql)
        total = conn.execute(
            "SELECT COUNT(*) FROM universal_accounts WHERE code = '1401'").fetchone()[0]
    finally:
        conn.close()
    assert [(c, t) for c, t, _ in rows] == [("1401", "asset")]
    assert "在制品" in (rows[0][2] or "")
    assert total == 1
