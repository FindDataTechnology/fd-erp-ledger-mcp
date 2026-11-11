"""事件两条车道的隔离与门禁（change: establish-erp-event-kernel 4.3 / 4.4）。

这两条是「拟稿车道不得绕过确认门」的机器判据，不靠代码评审记性：

  4.3 幂等键空间隔离：同 `stable_id` 在 ingest 与 draft 各落一条，互不去重。
  4.4 无旁路：全仓只有 `decide_event` / `reverse_event` 两个函数写
      `business_events.status`，且 `accepted` **只作为参数**传入（没有
      `status='accepted'` 字面量）——这样 pending 前置检查无法被一条硬编码
      UPDATE 绕过。迁移 SQL 里也不许出现 `SET status`（否则触发器旁边多一条暗门）。

4.4 的第二道判据（运行时）：pending 事件投影/过账被拒、被拒事件零影响，
由 `test_event_kernel_e2e.py::test_10_2` 覆盖（本文件不重复）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from fd_erp_ledger_mcp import events
from fd_erp_ledger_mcp.db import connection

PKG = Path(__file__).resolve().parent.parent / "fd_erp_ledger_mcp"
MIGRATIONS = PKG / "migrations"
TENANT = "acme"


def _sql_literals_with_function(root: Path) -> list[tuple[str, str, str]]:
    """扫出所有写 status 的 SQL 字面量 → [(文件, 所在函数, SQL 片段)]。"""
    found: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

            def visit_Constant(self, node: ast.Constant) -> None:
                value = node.value
                if isinstance(value, str) and "UPDATE business_events" in value \
                        and "status" in value:
                    found.append((str(path.relative_to(root)), stack[-1] if stack else "<module>",
                                  " ".join(value.split())))
                self.generic_visit(node)

        Visitor().visit(tree)
    return found


def test_4_3_same_stable_id_is_isolated_per_lane(settings):
    """同 stable_id 两条车道各落一条：幂等键空间不混用。"""
    with connection(settings) as conn:
        ingest = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
            stable_id="same-key", occurred_at="2026-11-12T00:00:00Z", lane="ingest",
            payload={"sku": "SKU-1", "quantity": 1, "unit": "pcs", "warehouse": "WH1"})
        draft = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_adjusted",
            stable_id="same-key", occurred_at="2026-11-12T00:00:00Z", lane="draft",
            payload={"sku": "SKU-1", "quantity": 1, "unit": "pcs", "reason": "盘盈"})

        assert ingest["id"] != draft["id"]
        assert ingest["status"] == "accepted" and draft["status"] == "pending"
        n = conn.execute("SELECT COUNT(*) FROM business_events WHERE stable_id='same-key'"
                         ).fetchone()[0]
        assert n == 2

        # 各自重放仍只去重自己那条车道
        again_ingest = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
            stable_id="same-key", occurred_at="2026-11-12T00:00:00Z", lane="ingest",
            payload={"sku": "SKU-1", "quantity": 1, "unit": "pcs", "warehouse": "WH1"})
        assert again_ingest["duplicate"] is True
        assert again_ingest["id"] == ingest["id"]


def test_4_4_only_the_gate_functions_write_event_status():
    """全仓只有确认门与冲正写 status；多出第三个写点即红（旁路入口）。"""
    hits = _sql_literals_with_function(PKG)
    owners = {fn for _, fn, _ in hits}
    assert owners == {"decide_event", "reverse_event"}, (
        "出现确认门之外的 business_events.status 写点：" + repr(hits))


def test_4_4_accepted_is_never_hardcoded_in_an_update():
    """`accepted` 必须走参数：硬编码 NEW 状态会绕过 pending 前置检查。"""
    hits = _sql_literals_with_function(PKG)
    hardcoded = [h for h in hits if "status='accepted'" in h[2].replace(" ", "")]
    assert hardcoded == [], f"硬编码 accepted 的直写点：{hardcoded}"
    # 正向对照：reversed 允许硬编码（它有自己的前置检查在函数里）
    assert any("status='reversed'" in sql.replace(" ", "") for _, _, sql in hits)


def test_4_4_migrations_cannot_flip_status_sql_side():
    """迁移/SQL 函数里不得出现 `SET status`——触发器旁边不许有暗门。"""
    offenders = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = " ".join(line.split())
            if stripped.startswith("--"):
                continue
            if "UPDATE business_events" in stripped and "SET status" in stripped:
                offenders.append(f"{path.name}: {stripped[:120]}")
    assert offenders == [], f"迁移里有直接改事件状态的 SQL：{offenders}"


def test_4_4_gate_rejects_non_pending_and_ingest_lane(settings):
    """门只在 draft 车道的 pending 事件上生效；ingest 车道不经门、非 pending 不重开。"""
    from fd_erp_ledger_mcp.errors import ErpError

    with connection(settings) as conn:
        ev = events.append_event(
            conn, settings=settings, tenant_id=TENANT, event_type="goods_received",
            stable_id="ingest-1", occurred_at="2026-11-12T00:00:00Z", lane="ingest",
            payload={"sku": "SKU-1", "quantity": 1, "unit": "pcs", "warehouse": "WH1"})
        for kwargs, expected in (
            ({"stable_id": "ingest-1", "lane": "ingest", "decision": "accepted"},
             "EVENT_STATE"),
            ({"stable_id": "missing", "lane": "draft", "decision": "accepted"},
             "NOT_FOUND"),
            ({"stable_id": "ingest-1", "lane": "ingest", "decision": "maybe"},
             "INVALID_ARGUMENT"),
        ):
            try:
                events.decide_event(conn, tenant_id=TENANT, actor="ops", **kwargs)
                raise AssertionError(f"不该通过：{kwargs}")
            except ErpError as exc:
                assert exc.code == expected, (kwargs, exc.code)
        # 门不改写 ingest 车道的事件
        again = conn.execute("SELECT status FROM business_events WHERE id=%s",
                            (ev["id"],)).fetchone()[0]
        assert again == "accepted"
