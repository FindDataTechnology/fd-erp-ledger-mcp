"""承载探针：`GET /health`（harden-erp-operations 4.1）。

锁三件事：
  1. 路由真的挂在 HTTP app 上（此前 `/health` `/healthz` `/` 全 404，没有任何健康面）；
  2. 不碰数据库 —— 探针会被集群反复打，且创造 server 时不连库；
  3. 不回显租户与 DSN（只报服务名与承载形态）。
"""
from __future__ import annotations

import json

import pytest

from fd_erp_ledger_mcp.config import Settings
from fd_erp_ledger_mcp.server import create_server


def _settings() -> Settings:
    # DSN 故意带上可识别的库名/用户，用于断言"不回显"
    return Settings(tenant_id="tenant-should-not-leak",
                    database_url="postgresql://leak_user:leak_pw@db.invalid:5432/leak_db")


@pytest.mark.no_db
def test_health_route_registered():
    app = create_server(_settings()).http_app()
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/health" in paths


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_health_returns_ok_without_leaking_identity():
    app = create_server(_settings()).http_app()
    payload = None
    for r in app.routes:
        if getattr(r, "path", "") == "/health":
            resp = await r.endpoint(None)
            payload = json.loads(resp.body)
            break
    assert payload is not None, "找不到 /health 端点"
    assert payload["ok"] is True
    assert payload["service"] == "fd-erp-ledger-mcp"
    blob = json.dumps(payload)
    for forbidden in ("tenant-should-not-leak", "leak_user", "leak_pw", "leak_db", "db.invalid"):
        assert forbidden not in blob, f"健康端点回显了不该出现的内容：{forbidden}"
