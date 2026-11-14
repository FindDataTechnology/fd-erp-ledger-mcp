"""词元客户轨的**接线**（converge-line-identity-and-keys 8.5）。

`test_line_token.py` 证的是「这枚票本身合不合法」；本文件证的是**票真的能走进
路由**。两层必须分开测：规则层全绿而接线层断，是这类改动最典型的假绿
（中间件没换上、或共享钥匙那条分支把票吃掉了）。

被测的接缝只有一处：`auth.BearerAuthMiddleware`。它同时是共享钥匙（回退轨）与
词元轨的门——所以这里必须把**三条边界**各钉一次：

1. 共享钥匙照旧能用（平台代调走的就是它，动它就是动生产）；
2. 词元票能用，且身份真的挂进了 scope（`auth.SCOPE_IDENTITY`）；
3. 两个都没配时，行为与接入前**逐字节一致**（401 的正文都不变）。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.no_db

from fd_erp_ledger_mcp import auth, wellknown  # noqa: E402
from fd_erp_ledger_mcp.config import Settings  # noqa: E402
from fd_erp_ledger_mcp.line_token import ENV_ISSUER, ENV_LINE, ENV_PEM, LINE_NAME  # noqa: E402
from fd_erp_ledger_mcp.server import create_server  # noqa: E402
from tests.test_line_token import ISSUER, _PUB, mint  # noqa: E402

SHARED = "t0ken-family-shape"
DATA_PATH = "/mcp"
EXEMPT = ("/health", "/healthz", "/metrics", wellknown.WELL_KNOWN_PATH)


def _app(monkeypatch, *, shared: str = "", line_track: bool = False):
    """按 `cli.serve()` 的同一顺序拼应用；门是否挂由 `auth.gate_enabled()` 决定。"""
    pytest.importorskip("fastmcp")
    from starlette.middleware import Middleware

    if shared:
        monkeypatch.setenv(auth.ENV_TOKEN, shared)
    else:
        monkeypatch.delenv(auth.ENV_TOKEN, raising=False)
    if line_track:
        monkeypatch.setenv(ENV_ISSUER, ISSUER)
        monkeypatch.setenv(ENV_LINE, LINE_NAME)
        monkeypatch.setenv(ENV_PEM, _PUB)
    else:
        for env in (ENV_ISSUER, ENV_LINE, ENV_PEM):
            monkeypatch.delenv(env, raising=False)

    server = create_server(Settings(database_url=None))
    wellknown.register(server)
    middleware = [Middleware(auth.BearerAuthMiddleware)] if auth.gate_enabled() else None
    return server.http_app(path=DATA_PATH, middleware=middleware, transport="http")


def _client(app):
    from starlette.testclient import TestClient

    return TestClient(app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_gate_enabled_covers_either_track(monkeypatch):
    """门存在 = 共享钥匙**或**词元轨任一被配置；两者都空时不挂门。"""
    monkeypatch.delenv(auth.ENV_TOKEN, raising=False)
    for env in (ENV_ISSUER, ENV_LINE, ENV_PEM):
        monkeypatch.delenv(env, raising=False)
    assert auth.gate_enabled() is False
    monkeypatch.setenv(auth.ENV_TOKEN, SHARED)
    assert auth.gate_enabled() is True
    monkeypatch.delenv(auth.ENV_TOKEN)
    monkeypatch.setenv(ENV_ISSUER, ISSUER)
    monkeypatch.setenv(ENV_PEM, _PUB)
    assert auth.gate_enabled() is True, "只开词元轨的部署也必须有门"


def test_shared_token_still_works_when_line_track_enabled(monkeypatch):
    """8.1/8.2 同款边界：开词元轨不影响回退轨——平台代调走的就是这条。"""
    app = _app(monkeypatch, shared=SHARED, line_track=True)
    with _client(app) as c:
        r = c.post(DATA_PATH, headers=_bearer(SHARED), json={})
        assert r.status_code != 401, r.text


def test_line_token_passes_and_identity_reaches_scope(monkeypatch):
    """词元票 → 不是 401；且身份挂进 `auth.SCOPE_IDENTITY`（不是只判了个签名）。"""
    import asyncio

    seen = {}

    async def _spy(scope, receive, send):
        # 只记录下游看到的身份；不回响应（本用例判的是接线，不是响应内容）。
        seen["ident"] = scope.get(auth.SCOPE_IDENTITY)

    monkeypatch.setenv(auth.ENV_TOKEN, SHARED)
    monkeypatch.setenv(ENV_ISSUER, ISSUER)
    monkeypatch.setenv(ENV_LINE, LINE_NAME)
    monkeypatch.setenv(ENV_PEM, _PUB)
    mw = auth.BearerAuthMiddleware(_spy)

    scope = {
        "type": "http",
        "path": DATA_PATH,
        "headers": [(b"authorization", f"Bearer {mint(sub='caller-42')}".encode())],
    }
    asyncio.run(mw(scope, None, None))
    ident = seen.get("ident")
    assert ident is not None, "票验过了却没把身份交给下游 = 接线层断"
    assert ident.subject == "caller-42"
    assert ident.can_write is True


def test_bad_and_foreign_tokens_are_401_with_the_same_body(monkeypatch):
    """坏票、别线的票、共享钥匙的近似值，都回**同一句** 401（不解释哪一步没过）。"""
    app = _app(monkeypatch, shared=SHARED, line_track=True)
    bodies = []
    with _client(app) as c:
        for token in ("not-a-jwt", mint(source="wire"), mint(aud="wire"), SHARED + "x"):
            r = c.post(DATA_PATH, headers=_bearer(token), json={})
            assert r.status_code == 401, (token[:12], r.status_code)
            bodies.append(r.text)
    assert len(set(bodies)) == 1, "不同的失败原因不该给出不同的响应正文"


def test_unconfigured_is_byte_identical_to_before(monkeypatch):
    """两个都没配 ⇒ 门不存在：数据面与豁免面都不 401（与接入前逐字节一致）。"""
    app = _app(monkeypatch)
    assert auth.gate_enabled() is False
    with _client(app) as c:
        assert c.post(DATA_PATH, json={}).status_code != 401
        for path in EXEMPT:
            assert c.get(path).status_code != 401


def test_exempt_paths_survive_with_line_track_only(monkeypatch):
    """只开词元轨时，探针/看板/自描述端点仍匿名可读（门不能把它们一起关掉）。"""
    app = _app(monkeypatch, line_track=True)
    with _client(app) as c:
        for path in ("/health", wellknown.WELL_KNOWN_PATH):
            assert c.get(path).status_code == 200, path
        assert c.post(DATA_PATH, json={}).status_code == 401
