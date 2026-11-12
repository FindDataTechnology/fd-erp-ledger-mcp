"""营销接入事件层 · 多板块生产者（change: fuse-marketing-onto-event-kernel 1.1-1.6）。

判据分七组，全部对着真实 Postgres 跑（DB 约束不可 mock）：

  1. 存量六类 ERP 类型仍落 producer='erp'（0011 的 ADD COLUMN 默认值，不做回填）。
  2. 三个营销类型可写入、按 producer 过滤正确、MCP 工具返回里 producer 正确。
  3. 类型名前缀与 producer 不自洽 → 拒（两个方向各一次），错误码可识别。
  4. 四类无载体事实（含 *_spend / lead_* / *_conversion 变体）→ 拒，码 FABRICATED_FACT。
  5. 营销类型缺 evidence_ref（含空白串）→ 拒；ERP 老类型不受影响。
  6. append_event 的 producer 形参不生效：板块归属只认类型注册表。
  7. 0011 迁移幂等：migrate 重放是 no-op，SQL 文件本身重复执行不报错。

为什么这些断言要落在 DB 层：注册侧闸是触发器，只在真实 Postgres 里存在；
在 Python 侧 mock 一个「校验函数」等于自己证明自己。
"""
from __future__ import annotations

import asyncio

import psycopg
import pytest

from fd_erp_ledger_mcp import events
from fd_erp_ledger_mcp.db import MIGRATIONS_DIR, connection, migrate
from fd_erp_ledger_mcp.error_codes import lookup
from fd_erp_ledger_mcp.errors import ErpError, map_psycog_error

TENANT = "acme"

ERP_LEGACY_TYPES = (
    "goods_received", "goods_issued", "goods_adjusted",
    "order_placed", "invoice_requested", "payment_received",
)

#: 三个营销类型 + 各自「适配器真能填出」的载荷（required 全部给实值）。
MARKETING_PAYLOADS = {
    "marketing.content.published": {
        "slug": "home", "title": "首页", "published_at": "2026-11-12T00:00:00Z",
        "artifact_ref": "art-1:v3",
    },
    "marketing.desk.conversation_closed": {
        "conversation_ref": "cw-1", "closed_at": "2026-11-12T00:00:00Z",
        "channel": "web",
    },
    "marketing.channel.order_paid": {
        "order_ref": "shopify-1", "amount": 128.5, "currency": "CNY",
        "channel": "shopify",
    },
}

_INSERT_TYPE = ("INSERT INTO event_types(event_type, description, producer, domain, evidence_ref) "
                "VALUES (%s, '', %s, NULL, %s)")


def _reject(dsn: str, event_type: str, producer: str, evidence_ref: str | None) -> ErpError:
    """直接用 SQL 登记一个类型，把 Postgres 抛的错翻译成封闭码（中间不夹 Python 校验）。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.Error) as ei:
            conn.execute(_INSERT_TYPE, (event_type, producer, evidence_ref))
    return map_psycog_error(ei.value)


# ---------------------------------------------------------------------------
# 1. 存量 ERP 类型
# ---------------------------------------------------------------------------

def test_legacy_erp_types_are_producer_erp(settings):
    """六类存量类型全部 producer='erp'，且没有 evidence_ref（老类型不需要载体出处）。"""
    with connection(settings) as conn:
        rows = dict(conn.execute(
            "SELECT event_type, producer FROM event_types WHERE event_type = ANY(%s)",
            (list(ERP_LEGACY_TYPES),)).fetchall())
    assert rows == {t: "erp" for t in ERP_LEGACY_TYPES}

    with connection(settings) as conn:
        refs = [r[0] for r in conn.execute(
            "SELECT evidence_ref FROM event_types WHERE event_type = ANY(%s)",
            (list(ERP_LEGACY_TYPES),)).fetchall()]
    assert refs == [None] * len(ERP_LEGACY_TYPES)


# ---------------------------------------------------------------------------
# 2. 营销类型可写入 + 过滤 + 工具返回
# ---------------------------------------------------------------------------

def test_marketing_events_are_writable_and_filterable_by_producer(settings):
    with connection(settings) as conn:
        for event_type, payload in MARKETING_PAYLOADS.items():
            occurred_at = payload.get("published_at") or payload.get("closed_at") or (
                "2026-11-12T00:00:00Z")
            ev = events.append_event(
                conn, settings=settings, tenant_id=TENANT, event_type=event_type,
                stable_id=f"mkt-{event_type}", occurred_at=occurred_at,
                payload=payload, lane="ingest", source_ref="test-fixture")
            assert ev["producer"] == "marketing", event_type
            assert ev["status"] == "accepted"

        # 同租户再写一条 ERP 事件：过滤不能靠「表里只有营销事件」这种巧合成立
        erp = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
            stable_id="erp-1", occurred_at="2026-11-12T00:00:00Z", lane="ingest",
            payload={"sku": "SKU-1", "quantity": 1, "unit": "pcs", "warehouse": "WH1"})
        assert erp["producer"] == "erp"

        only_mkt = events.query_events(conn, tenant_id=TENANT, producer="marketing")
        assert {i["event_type"] for i in only_mkt["items"]} == set(MARKETING_PAYLOADS)
        assert only_mkt["completeness"]["declared_total"] == 3
        assert all(i["producer"] == "marketing" for i in only_mkt["items"])

        only_erp = events.query_events(conn, tenant_id=TENANT, producer="erp")
        assert [i["event_type"] for i in only_erp["items"]] == ["goods_received"]

        # 默认不过滤：同租户跨板块可见（D6 不设板块墙）
        both = events.query_events(conn, tenant_id=TENANT)
        assert len(both["items"]) == 4
        assert {i["producer"] for i in both["items"]} == {"erp", "marketing"}


def test_rpc_query_events_carries_producer(settings):
    """工具面同样带 producer（agent 靠它分板块，不能只靠类型名前缀猜）。"""
    from fastmcp import Client
    from fd_erp_ledger_mcp.server import create_server

    server = create_server(settings)
    payload = MARKETING_PAYLOADS["marketing.content.published"]

    async def go():
        async with Client(server) as client:
            await client.call_tool("ingest_event", {
                "event_type": "marketing.content.published",
                "stable_id": "rpc-mkt-1",
                "occurred_at": payload["published_at"],
                "payload": payload,
                "source_ref": "rpc-test",
            })
            return await client.call_tool("query_events", {"producer": "marketing"})

    out = asyncio.run(go())
    data = out.data if hasattr(out, "data") else out
    assert data["ok"] is True
    assert [row["producer"] for row in data["data"]] == ["marketing"]
    assert data["data"][0]["event_type"] == "marketing.content.published"


# ---------------------------------------------------------------------------
# 3. 前缀与 producer 自洽
# ---------------------------------------------------------------------------

def test_type_prefix_must_match_producer(settings):
    err = _reject(settings.database_url, "foo.bar", "marketing", "ref:test")
    assert err.code == "EVENT_PRODUCER_MISMATCH", err.message
    assert lookup("EVENT_PRODUCER_MISMATCH").semantic_class == "invalid_argument"

    err = _reject(settings.database_url, "marketing.x.y", "erp", "ref:test")
    assert err.code == "EVENT_PRODUCER_MISMATCH", err.message

    # 正向对照：自洽的两个方向都能登记（否则上面的红可能来自「这条 SQL 根本写不进去」）
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(_INSERT_TYPE, ("marketing.test.ok", "marketing", "ref:test"))
        conn.execute(_INSERT_TYPE, ("erp.test.ok", "erp", None))
        conn.execute("DELETE FROM event_types WHERE event_type IN "
                     "('marketing.test.ok', 'erp.test.ok')")


# ---------------------------------------------------------------------------
# 4. 四类无载体事实黑名单
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event_type, producer", [
    ("marketing.ad.campaign_spend", "marketing"),          # 投放花费
    ("marketing.desk.lead_created", "marketing"),          # 线索漏斗
    ("marketing.site.campaign_launched", "marketing"),     # 活动
    ("marketing.retail.checkout_conversion", "marketing"),  # 转化（*_conversion 写法）
    ("marketing.ads.spend", "marketing"),                  # *_spend 变体
    ("lead_capture", "erp"),                               # lead_* 变体，与 producer 无关
    # 词干夹在中间（2026-11-12 实测漏过）：只锚后缀会放过下面这几个
    ("marketing.ad_spend_monthly", "marketing"),
    ("marketing.campaign.performance", "marketing"),
    ("marketing.leads.weekly_count", "marketing"),
    ("marketing.site.conversions_30d", "marketing"),
])
def test_fabricated_fact_names_are_rejected(settings, event_type, producer):
    err = _reject(settings.database_url, event_type, producer, "ref:test")
    assert err.code == "EVENT_FABRICATED_FACT", err.message
    assert "先补载体再登记类型" in err.message
    assert lookup("EVENT_FABRICATED_FACT").semantic_class == "invalid_argument"


# ---------------------------------------------------------------------------
# 5. 营销类型必须指出载体
# ---------------------------------------------------------------------------

def test_marketing_type_requires_evidence_ref(settings):
    for missing in (None, "   "):
        err = _reject(settings.database_url, "marketing.content.draft_created",
                      "marketing", missing)
        assert err.code == "EVENT_FABRICATED_FACT", err.message
        assert "evidence_ref" in err.message

    # ERP 老类型不受这条影响：evidence_ref 可空
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(_INSERT_TYPE, ("erp.test.no_evidence", "erp", None))
        conn.execute("DELETE FROM event_types WHERE event_type = 'erp.test.no_evidence'")


# ---------------------------------------------------------------------------
# 6. producer 只认注册表，调用方声明不生效
# ---------------------------------------------------------------------------

def test_append_event_ignores_caller_declared_producer(settings):
    mkt_payload = MARKETING_PAYLOADS["marketing.desk.conversation_closed"]
    with connection(settings) as conn:
        # 营销类型自称 erp → 仍归 marketing
        mkt = events.append_event(
            conn, settings=settings, tenant_id=TENANT,
            event_type="marketing.desk.conversation_closed", stable_id="declare-1",
            occurred_at=mkt_payload["closed_at"], payload=mkt_payload, lane="ingest",
            producer="erp")
        assert mkt["producer"] == "marketing"

        # ERP 类型自称 marketing → 仍归 erp
        erp = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
            stable_id="declare-2", occurred_at="2026-11-12T00:00:00Z", lane="ingest",
            payload={"sku": "SKU-1", "quantity": 1, "unit": "pcs", "warehouse": "WH1"},
            producer="marketing")
        assert erp["producer"] == "erp"

        # 结构性保证：business_events 根本没有 producer 列，事件层面无从声明板块
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'business_events'").fetchall()}
        assert "producer" not in cols


# ---------------------------------------------------------------------------
# 7. 迁移幂等
# ---------------------------------------------------------------------------

def test_migration_0011_is_idempotent(settings):
    # ① 版本已登记 → migrate 本次零应用（re-apply = no-op）
    assert migrate(settings)["applied"] == []

    # ② migrate 会跳过已登记版本，故直接把 SQL 文件重放两遍，证明 DDL 本身幂等
    sql = (MIGRATIONS_DIR / "0011_marketing_producers.sql").read_text(encoding="utf-8")
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(sql)
        conn.execute(sql)
        assert conn.execute(
            "SELECT COUNT(*) FROM event_types WHERE producer = 'marketing'").fetchone()[0] == 3
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations").fetchone()[0] == "0011_marketing_producers"
