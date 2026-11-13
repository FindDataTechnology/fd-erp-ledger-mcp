"""共享 bearer 门的判据（change converge-line-identity-and-keys 8.5）。

判据都对着**这个门自己**，不拿邻近信号冒充：

1. 未配 ``MCP_BEARER_TOKEN`` 时门**不存在**——与接入前逐字节一致（不挂任何中间件）；
2. 配了之后 ``/mcp``：匿名 **401**、假令牌 **401**、真令牌 **不是 401**；
3. 三条豁免面（``/health`` ``/healthz`` ``/metrics``）**永远 200** —— 它们分别对应
   liveness 探针、家族约定与 ops-console 直连 ClusterIP 抓的看板数据源，都不经网关、
   不带钥匙。门掉其中任何一条都是**静默**故障：pod 被反复重启，或 ERP 看板变空白；
4. 豁免是**精确路径**，不是前缀——否则 ``/health/../mcp`` 之类的形状会被放过；
5. ``cli.serve()`` 的分支选择：配了令牌才把门作为 middleware 传下去，并用
   ``http_app`` 起 uvicorn（而不是 ``run()``，后者不接中间件）。
"""
from __future__ import annotations

import pytest

from fd_erp_ledger_mcp import auth, wellknown
from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.server import create_server

TOKEN = "t0ken-family-shape"

#: 本文件全是纯 ASGI/分支判据，零数据库往返——挂 `no_db` 免得被 conftest 的
#: 逐用例 TRUNCATE 前置整体 skip 掉（skip 掉的判据等于没有，harden-erp-operations 4.1
#: 已为此加过这条豁免）。
pytestmark = pytest.mark.no_db

#: 数据面：必须被门挡。
DATA_PATH = "/mcp"
#: 豁免面：探针 / 家族约定 / 运维看板 / 线自描述端点（1.4 补的第二处投影）。
EXEMPT = ("/health", "/healthz", "/metrics", wellknown.WELL_KNOWN_PATH)


def _app(monkeypatch, token: str):
    """按 ``cli.serve()`` 的同一顺序拼应用（不调 serve：它读环境、绑端口并阻塞）。"""
    pytest.importorskip("fastmcp")
    from starlette.middleware import Middleware

    monkeypatch.setenv(auth.ENV_TOKEN, token)
    server = create_server(Settings(database_url=None))
    # 与 cli.serve() 同序：自描述端点在 http_app() 之前挂（1.4 的第二处投影）。
    wellknown.register(server)
    middleware = [Middleware(auth.BearerAuthMiddleware)] if auth.configured_token() else None
    return server.http_app(path=DATA_PATH, middleware=middleware, transport="http")


def _client(app):
    """`with` 进入：FastMCP 的 http_app 靠 lifespan 建 task group，不进上下文打 `/mcp`
    会得 `RuntimeError: Task group is not initialized`（与本门无关，是 MCP 层的前置）。"""
    from starlette.testclient import TestClient

    return TestClient(app)


# --------------------------------------------------------------------------- 门本身


def test_exempt_paths_are_exactly_probe_and_dashboard():
    """豁免面就是这三条；数据面不在其中（把 /mcp 加进去等于没有门）。"""
    assert auth.EXEMPT_PATHS == EXEMPT
    assert DATA_PATH not in auth.EXEMPT_PATHS


def test_token_is_read_per_request(monkeypatch):
    """每请求重读：轮换令牌不需要重启进程。"""
    monkeypatch.delenv(auth.ENV_TOKEN, raising=False)
    assert auth.configured_token() == ""
    monkeypatch.setenv(auth.ENV_TOKEN, "  spaced  ")
    assert auth.configured_token() == "spaced"  # 空白会被 strip


def test_no_token_means_no_gate(monkeypatch):
    """未配令牌时门不存在——这是"接入前逐字节一致"的可执行表述。"""
    app = _app(monkeypatch, "")
    with _client(app) as client:
        assert client.post(DATA_PATH, json={}).status_code != 401, "未配令牌时不应有门"


# --------------------------------------------------------------------- 真 ASGI 装配


def test_anonymous_data_path_is_401(monkeypatch):
    app = _app(monkeypatch, TOKEN)
    res = _client(app).post(DATA_PATH, json={})
    assert res.status_code == 401
    assert res.json() == {"error": "unauthorized"}
    assert res.headers.get("www-authenticate") == "Bearer"


def test_wrong_token_is_401(monkeypatch):
    app = _app(monkeypatch, TOKEN)
    res = _client(app).post(DATA_PATH, json={}, headers={"authorization": "Bearer nope"})
    assert res.status_code == 401


def test_bare_token_without_scheme_is_401(monkeypatch):
    """恒时比较的是完整头部值：裸令牌（缺 `Bearer ` 前缀）必须被拒。"""
    app = _app(monkeypatch, TOKEN)
    res = _client(app).post(DATA_PATH, json={}, headers={"authorization": TOKEN})
    assert res.status_code == 401


def test_correct_token_passes_the_gate(monkeypatch):
    """真令牌不该再被门挡；具体状态码由 MCP 层决定（非法体可以 4xx，但不能 401）。"""
    app = _app(monkeypatch, TOKEN)
    with _client(app) as client:
        res = client.post(DATA_PATH, json={}, headers={"authorization": f"Bearer {TOKEN}"})
        assert res.status_code != 401


@pytest.mark.parametrize("path", EXEMPT)
def test_exempt_paths_stay_open_with_a_gate(monkeypatch, path):
    """豁免的判据是「不被门挡」，不是「一定 200」——`/healthz` 本线没有这条路由，
    404 也说明门放它过去了；`/health` 与 `/metrics` 必须 200。"""
    app = _app(monkeypatch, TOKEN)
    with _client(app) as client:
        res = client.get(path)
    assert res.status_code != 401, f"{path} 被门挡了（探针/看板会静默坏掉）"
    if path != "/healthz":
        assert res.status_code == 200, path


def test_exempt_match_is_exact_not_prefix(monkeypatch):
    """`/healthz/mcp`、`/metrics/mcp` 这类形状不该借豁免进来。"""
    app = _app(monkeypatch, TOKEN)
    with _client(app) as client:
        for path in ("/healthz/mcp", "/metrics/mcp"):
            assert client.post(path, json={}).status_code == 401, path


def test_lifespan_is_not_blocked(monkeypatch):
    """门只拦 http scope；lifespan 被挡会让应用起不来。"""
    scope = {"type": "lifespan"}
    seen = []

    def _inner(scope, receive, send):
        seen.append(scope["type"])

    auth.BearerAuthMiddleware(_inner)(scope, None, None)
    assert seen == ["lifespan"]


# ------------------------------------------------------------------ 接线（分支选择）


def test_serve_wires_middleware_only_when_token_configured(monkeypatch):
    """`cli.serve()` 必须：配了令牌才传 middleware，且用 http_app（run() 不接中间件）。"""
    import uvicorn

    captured: dict = {}

    class _FakeServer:
        def run(self, **kw):  # pragma: no cover - 走 http 分支时不该被调用
            raise AssertionError("配了令牌还走 run()：中间件没接上")

        def custom_route(self, path, methods=None):  # 真 FastMCP 有；1.4 的自描述端点要用
            def deco(fn):
                captured.setdefault("custom_routes", []).append(path)
                return fn

            return deco

        def http_app(self, **kw):
            captured.update(kw)
            return object()

    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update({"uvicorn": kw}))
    monkeypatch.setattr(
        "fd_erp_ledger_mcp.server.create_server", lambda settings=None: _FakeServer()
    )
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    from fd_erp_ledger_mcp.cli import serve

    serve.__wrapped__() if hasattr(serve, "__wrapped__") else None
    # click 命令包装：直接取底层回调，避免 Click 解析
    serve.callback(host="127.0.0.1", port=8000, transport="http", skip_schema_check=True)

    assert captured["path"] == "/mcp"
    assert captured.get("custom_routes") == [wellknown.WELL_KNOWN_PATH]
    assert captured["middleware"] and captured["middleware"], "配了令牌却没接中间件"
    assert "uvicorn" in captured
    assert uvicorn is not None


def test_serve_without_token_keeps_old_shape(monkeypatch):
    """未配令牌：middleware 必须是 None（= 与接入前逐字节一致）。"""
    captured: dict = {}

    class _FakeServer:
        def run(self, **kw):  # pragma: no cover
            raise AssertionError

        def custom_route(self, path, methods=None):
            def deco(fn):
                captured.setdefault("custom_routes", []).append(path)
                return fn

            return deco

        def http_app(self, **kw):
            captured.update(kw)
            return object()

    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update({"uvicorn": kw}))
    monkeypatch.setattr(
        "fd_erp_ledger_mcp.server.create_server", lambda settings=None: _FakeServer()
    )
    monkeypatch.delenv(auth.ENV_TOKEN, raising=False)

    from fd_erp_ledger_mcp.cli import serve

    serve.callback(host="127.0.0.1", port=8000, transport="http", skip_schema_check=True)

    assert captured["middleware"] is None
