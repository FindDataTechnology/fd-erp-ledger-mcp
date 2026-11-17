"""Postgres 访问层 + 迁移阶段（db-schema-lifecycle）。

- 应用运行时只发 DML；任何 CREATE/ALTER/DROP 只在 `migrate()` 阶段发生。
- `migrate()` 一次性、可重放：已登记版本跳过（re-apply 是 no-op），
  不重跑任何 DDL。DDL 本身幂等，故既有库可 adopt 而无需重放历史。
- `assert_schema_version()` 在启动/首次连接时比对库内记录版本与代码要求版本，
  落后即 fail-fast（同时报出 required 与 actual，指向迁移阶段）。
"""
from __future__ import annotations

import dataclasses
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .errors import ErpError
from .tenancy import DEFAULT_TENANT, PUBLIC_SCHEMA, TENANT_SCHEMA_PREFIX, schema_for

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
#: 0013：分析维度——proposals / journal_entries 各增 dimensions JSONB（默认空集）+ GIN 索引；
#:       键是封闭集（department/project/channel/cost_center/entity），值是自由字符串。
#:       纯追加、不回填历史（未标注就是空集，不猜归属）；既有账务语义（借贷必平/幂等/余额重算）不变。
#:       注：jsonb 存储、无 DB CHECK，故后续加键（customer / production_order）只改
#:       `dimensions.DIMENSION_KEYS`，不动表结构——加键仍是受审的代码改动，不是数据迁移。
#: 0014：业务日期 + 期间关账（change: add-period-close）——proposals / journal_entries 各增
#:       booked_on DATE（历史行按显式 Asia/Shanghai 从 posted_at 回填），accounting_periods
#:       追加式期间台账（close/reopen + 关账快照），journal_entries 落账闸（已关期间拒收）。
#:       booked_on 是 DATE 而非 timestamptz：期间归属因此与会话时区彻底解耦（0010 的教训）。
#: 0015：通用科目补坏账准备 1231（抵减应收）与信用减值损失 6602
#:       （change: add-bad-debt-provision）。纯追加 + 幂等，无数据迁移。
#: 0016：通用科目补汇兑损益 6603（期间费用，进利润表与未结转损益，不混进 5200 经营费用）
#:       （change: add-fx-revaluation）。纯追加 + 幂等，无数据迁移。
#: 0017：通用科目补生产成本 1401（在制品，change: add-manufacturing-orders）。
#:       纯追加 + 幂等，无数据迁移；既有租户须重跑 seed-accounts。
#: 0018：通用科目补增值税六科目（change: add-cn-localization-pack）——2221 应交税费 /
#:       2221.01 应交增值税 / 2221.01.01 销项税额 / 2221.01.02 进项税额 /
#:       2221.01.07 转出未交增值税 / 2221.02 未交增值税。科目码取自 localization/cn 数据包
#:       实测（2221.01.05 是出口退税，不是转出未交增值税）。纯追加 + 幂等。
#: 0019：通用科目补国别报表四科目（change: add-cn-localization-pack）——5301 营业外收入 /
#:       5403 税金及附加 / 5711 营业外支出 / 5801 所得税费用。没有它们，小企业准则的利润表
#:       出不了「净利润」（所得税无处可记，净利润永远等于利润总额）。纯追加 + 幂等。
#: 0020：通用科目补附加税与所得税四科目（change: add-cn-surcharge-and-income-tax）——
#:       2221.11 应交企业所得税 / 2221.12 城建税 / 2221.13 教育费附加 / 2221.14 地方教育附加。
#:       没有它们，附加税只能挤进「其他经营费用」（报表项目对不上），所得税无处可挂。
#: 0021：通用科目补进项税额转出（change: add-cn-red-letter-and-input-transfer-out）——
#:       2221.01.04 应交增值税（进项税额转出）。已抵扣进项在集体福利/非正常损失/免税或简易
#:       计税时须转出；没有该科目，应纳增值税写不成「销项 − 进项 + 转出」，附加税基数跟着错。
REQUIRED_SCHEMA_VERSION = "0021_cn_input_transfer_out_account"

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
    """惰性连接池（线程安全，供后台对账 worker 复用同一池）。

    `search_path` 的设定位置是**池构造期**而不是每次借出（design D3 的修正）：
    `settings.tenant_id` 每进程恒定，所以每条连接一出生就定向到本租户 schema，
    不需要逐请求 `SET`——少一处「忘了重置」的失败面。`options` 里的 schema 名由
    `schema_for()` 白名单校验，不是拼用户输入。

    **缓存键必须带 schema**：只按 dsn 缓存的话，同一进程里第二个租户会复用第一个
    租户的池、`search_path` 静默串到别人家 —— 这正是本 change 要消灭的那类
    「不报错的串数据」。`(dsn, schema)` 两元组是这条不变量的表达式。
    """
    dsn = _dsn(settings)
    schema = schema_for(settings.tenant_id)
    key = f"{dsn}\x00{schema}"
    with _lock:
        pool = _pools.get(key)
        if pool is None:
            from psycopg_pool import ConnectionPool
            # autocommit=True 让每个 `conn.transaction()` 成为真实顶层事务，
            # DEFERRABLE 借贷必平约束才能在落账事务边界（而非连接池归还时）触发。
            pool = ConnectionPool(dsn, min_size=1, max_size=8, timeout=10, open=True,
                                  name=f"erp-ledger:{schema}",
                                  kwargs={"autocommit": True,
                                          "options": f"-c search_path={schema}"})
            _pools[key] = pool
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


def close_pool(settings: Settings) -> None:
    """关闭并移除该租户的池（运维/测试用，例如临时库用完要 DROP DATABASE）。

    存在的理由：池的键现在是 `(dsn, schema)` 而不是裸 dsn，调用方**不该**自己去
    `_pools` 里拼键——拼错了池就关不掉，而症状是 `DROP DATABASE` 报「还有其它
    会话在用这个库」，看起来像并发或权限问题，其实是句柄没关。
    """
    schema = schema_for(settings.tenant_id)
    key = f"{_dsn(settings)}\x00{schema}"
    with _lock:
        pool = _pools.pop(key, None)
    if pool is not None:
        pool.close()


# ---------------------------------------------------------------------------
# 租户 schema 生命周期
# ---------------------------------------------------------------------------

def create_schema(settings: Settings) -> str:
    """把 `settings` 指向的租户 schema 建出来（幂等），返回 schema 名。

    `public` 是库内置的（租户 0），直接返回、不发任何 DDL——生产零变更是这条
    短路保证的，不是「碰巧没执行到」。

    连接用的是**本租户的池**：此时 schema 还不存在，`SET search_path` 指向一个
    不存在的 schema 在 PG 里是合法的（它只是一串候选名），所以可以在这条连接上
    直接 `CREATE SCHEMA`，随后 `migrate()` 就在同一 schema 内建表。
    """
    schema = schema_for(settings.tenant_id)
    if schema == PUBLIC_SCHEMA:
        return schema
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                # schema 名经白名单校验（[a-z0-9_]），加引号后拼进 DDL 是安全的；
                # DDL 位置无法用参数占位符，这是唯一可行的写法。
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    finally:
        get_pool(settings).putconn(conn)
    return schema


def drop_schema(settings: Settings) -> None:
    """删掉 `settings` 指向的租户 schema（含其全部对象），并留审计痕。

    **拒绝 public**：租户 0 就是生产库本体，误删等于抹库。CLI 已拦一道，这里再拦
    一道——运维脚本直接调本函数时不该绕过保护。

    `CASCADE` 是刻意的：租户 schema 内的表互相有外键，逐表 DROP 要排序且漏一张就
    留残留；`DROP SCHEMA ... CASCADE` 是 PG 保证的全清。

    审计走**连到 public 的连接**（租户 0 的池）：被删的 schema 已不存在，往它里面
    写审计行必然失败；而「删了谁」这件事本身必须留痕。
    """
    from .tenancy import PUBLIC_SCHEMA

    schema = schema_for(settings.tenant_id)
    if schema == PUBLIC_SCHEMA:
        raise ErpError("TENANT_ZERO_PROTECTED", "public 是租户 0 的 schema，拒绝删除",
                       hint="租户 0 代表库本体，删除它等于抹库")
    conn = get_pool(settings).getconn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        get_pool(settings).putconn(conn)
    # 审计留痕：被删的 schema 已不可写，所以要找一个**存在 audit 表**的 schema 写。
    # 优先租户 0（`public`）——但「租户 0 一定 migrate 过」是个不成立的假设：测试库与
    # 新建库里 `public` 常常是空的（所有表都建在租户 schema 内）。所以先探，探不到
    # 就退到任一现有租户 schema；全都探不到时——删除已经生效，审计写不进去不该
    # 让调用方误以为「没删成」，因此记一条警告而不是抛错。
    _audit_drop(settings, schema)


def _audit_drop(settings: Settings, dropped_schema: str) -> None:
    """把 `tenant_dropped` 写进任一可写的 audit 表（见 `drop_schema` 的注释）。"""
    from .tenancy import audit as _audit

    candidates: list[str] = [DEFAULT_TENANT]
    try:
        conn = get_pool(settings).getconn()
        try:
            rows = fetch_all(
                conn,
                "SELECT nspname FROM pg_namespace WHERE nspname = %s OR nspname LIKE %s",
                (PUBLIC_SCHEMA, f"{TENANT_SCHEMA_PREFIX}%"),
            )
        finally:
            get_pool(settings).putconn(conn)
        for r in rows:
            name = r["nspname"]
            tid = DEFAULT_TENANT if name == PUBLIC_SCHEMA else name[len(TENANT_SCHEMA_PREFIX):]
            if tid not in candidates:
                candidates.append(tid)
    except Exception:  # noqa: BLE001 - 候选探测失败不阻断审计尝试
        pass

    for tenant in candidates:
        probe = dataclasses.replace(settings, tenant_id=tenant)
        try:
            aconn = get_pool(probe).getconn()
        except Exception:  # noqa: BLE001
            continue
        try:
            # audit 表不在这个 schema 里就抛，换下一个候选。
            _audit(aconn, tenant, "tenant_dropped",
                   target=dropped_schema,
                   detail={"schema": dropped_schema,
                           "tenant_id": settings.tenant_id,
                           "audited_in": tenant})
            return
        except Exception:  # noqa: BLE001
            continue
        finally:
            try:
                get_pool(probe).putconn(aconn)
            except Exception:  # noqa: BLE001
                pass
    import logging

    logging.getLogger(__name__).warning(
        "tenant %s dropped (schema %s) but no writable audit table was found — "
        "the drop took effect; audit trail is missing", settings.tenant_id, dropped_schema
    )


def list_tenant_schemas(settings: Settings) -> dict[str, Any]:
    """列出全部租户 schema。

    `public` 恒在（租户 0），单独标出；其余按 `tenant_` 前缀认。**不读注册表**——
    schema 清单本身就是权威（design D6）。
    """
    conn = get_pool(settings).getconn()
    try:
        rows = fetch_all(
            conn,
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname = %s OR nspname LIKE %s ORDER BY nspname",
            (PUBLIC_SCHEMA, f"{TENANT_SCHEMA_PREFIX}%"),
        )
    finally:
        get_pool(settings).putconn(conn)
    names = [r["nspname"] for r in rows]
    return {
        "schemas": names,
        "tenant_zero": PUBLIC_SCHEMA if PUBLIC_SCHEMA in names else None,
        "tenants": [
            {"schema": n, "tenant_id": n[len(TENANT_SCHEMA_PREFIX):]}
            for n in names if n.startswith(TENANT_SCHEMA_PREFIX)
        ],
    }


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
    """fail-fast：库内记录版本落后于代码要求即拒绝服务，同时点名两侧版本。

    查的是**连接当前 schema**（`current_schema()`，即池给这条连接设的
    `search_path` 首个元素）而不是写死 `public`——schema 隔离后每个租户有自己的一份
    `schema_migrations`，写死 public 会让租户 N 的进程拿租户 0 的版本号当自己的，
    于是「缺列的库」也能通过启动闸，故障推迟到运行时的某次 INSERT 上。
    """
    conn = get_pool(settings).getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_schema()")
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name='schema_migrations'"
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
