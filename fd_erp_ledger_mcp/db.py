"""Postgres 访问层 + 迁移阶段（db-schema-lifecycle）。

- 应用运行时只发 DML；任何 CREATE/ALTER/DROP 只在 `migrate()` 阶段发生。
- `migrate()` 一次性、可重放：已登记版本跳过（re-apply 是 no-op），
  不重跑任何 DDL。DDL 本身幂等，故既有库可 adopt 而无需重放历史。
- `assert_schema_version()` 在启动/首次连接时比对库内记录版本与代码要求版本，
  落后即 fail-fast（同时报出 required 与 actual，指向迁移阶段）。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .errors import ErpError

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
#: 代码要求的最低 schema 版本。加迁移必须同步提这里，否则闸门放行一个缺列的库，
#: 故障会推迟到运行时的某次 INSERT 上（报错点离原因很远）。
#: 0004：credit_reservations 增 expires_at / released_by / release_reason
#: （change: close-orphan-credit-reservations）。
#: 0005：fee_details 增 original_amount / base_amount / fx_rate / fx_as_of / fx_source
#:       与收费项登记表 billing_fee_items（change: optimize-erp-solution 5.1/5.5）。
#: 0006：recon_reports 增 request（对账任务重启续跑依据；同上 change 6.5）。
#: 0007：事件层 business_events / event_types / event_revisions + 只追加与定型触发器
#:       （change: establish-erp-event-kernel 1.1/1.2）。
#: 0008：数量面 objects / object_events / object_balances + 投影入口与重算
#:       （同上 change 2.1-2.3）。
#: 0009：过账 posting_rules / posting_links / posting_gaps（同上 change 3.1-3.4）。
#: 0010：定型边界时区显式化 event_types.period_timezone + erp_event_is_finalized 改签名
#:       （同上 change；修「定型时刻随会话时区漂移」的缺陷，见该迁移注释）。
#: 0011：事件类型注册表扩成多板块生产者——event_types 增 producer / domain / evidence_ref，
#:       登记三个营销类型（内容发布/客服会话关闭/渠道订单付款），加注册侧三道闸
#:       （前缀与 producer 自洽、四类无载体事实黑名单、营销类型须带载体出处）。
#: 0012：通用科目补固定资产业务两科目——1601 固定资产 / 1602 累计折旧（抵减资产，
#:       封闭的 account_type 无 contra-asset，按 asset 登记靠 note 与贷方方向表达抵减）。
#:       纯追加 + 幂等（ON CONFLICT (code) DO NOTHING），无数据迁移；
#:       既有租户须重跑内核 CLI seed-accounts 才把两科目种进各自 accounts。
REQUIRED_SCHEMA_VERSION = "0012_fixed_assets_accounts"

_lock = threading.Lock()
_pools: dict[str, Any] = {}


def _dsn(settings: Settings) -> str:
    if not settings.database_url:
        raise ErpError(
            "UPSTREAM_UNAVAILABLE",
            "未配置账本数据源：缺少环境变量 ERP_LEDGER_DATABASE_URL",
        )
    return settings.database_url


def get_pool(settings: Settings) -> Any:
    """惰性连接池（线程安全，供后台对账 worker 复用同一池）。"""
    dsn = _dsn(settings)
    with _lock:
        pool = _pools.get(dsn)
        if pool is None:
            from psycopg_pool import ConnectionPool
            # autocommit=True 让每个 `conn.transaction()` 成为真实顶层事务，
            # DEFERRABLE 借贷必平约束才能在落账事务边界（而非连接池归还时）触发。
            pool = ConnectionPool(dsn, min_size=1, max_size=8, timeout=10, open=True,
                                  name="erp-ledger", kwargs={"autocommit": True})
            _pools[dsn] = pool
        return pool


@contextmanager
def connection(settings: Settings) -> Iterator[Any]:
    """取一条连接；autocommit=False，由调用方用事务块控制提交。"""
    pool = get_pool(settings)
    with pool.connection() as conn:
        yield conn


def fetch_all(conn: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_one(conn: Any, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = fetch_all(conn, sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 迁移阶段（唯一发出 DDL 的地方）
# ---------------------------------------------------------------------------

def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def migrate(settings: Settings) -> dict[str, Any]:
    """一次性、可重放、幂等。返回本次实际应用的版本列表。"""
    applied_now: list[str] = []
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                cur.execute("SELECT version FROM schema_migrations")
                done = {r[0] for r in cur.fetchall()}
                for path in _migration_files():
                    version = path.stem
                    if version in done:
                        continue  # re-apply = no-op，不重跑任何 DDL
                    cur.execute(path.read_text(encoding="utf-8"))
                    cur.execute(
                        "INSERT INTO schema_migrations(version) VALUES (%s) "
                        "ON CONFLICT (version) DO NOTHING", (version,)
                    )
                    applied_now.append(version)
    finally:
        get_pool(settings).putconn(conn)
    return {"applied": applied_now, "required": REQUIRED_SCHEMA_VERSION}


def mark_adopted(settings: Settings, version: str = REQUIRED_SCHEMA_VERSION) -> dict[str, Any]:
    """既有库 adopt：仅登记版本、不执行任何数据定义语句（运维显式调用）。"""
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                cur.execute(
                    "INSERT INTO schema_migrations(version) VALUES (%s) "
                    "ON CONFLICT (version) DO NOTHING", (version,)
                )
    finally:
        get_pool(settings).putconn(conn)
    return {"adopted": version, "ddl_executed": False}


def assert_schema_version(settings: Settings) -> None:
    """fail-fast：库内记录版本落后于代码要求即拒绝服务，同时点名两侧版本。"""
    conn = get_pool(settings).getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='schema_migrations'"
            )
            if cur.fetchone() is None:
                raise ErpError(
                    "SCHEMA_OUTDATED",
                    "数据库未登记任何 schema 版本",
                    detail={"required": REQUIRED_SCHEMA_VERSION, "actual": None},
                )
            cur.execute("SELECT max(version) FROM schema_migrations")
            actual = cur.fetchone()[0]
    finally:
        get_pool(settings).putconn(conn)
    if actual is None or actual < REQUIRED_SCHEMA_VERSION:
        raise ErpError(
            "SCHEMA_OUTDATED",
            "数据库 schema 版本落后于代码要求",
            detail={"required": REQUIRED_SCHEMA_VERSION, "actual": actual},
        )
