"""HTTP 层共享 bearer 门——本线自有的常命钥匙（change converge-line-identity-and-keys 8.5）。

门只在 ``MCP_BEARER_TOKEN`` 非空时**存在**：未配置时服务入口仍走原来的
``server.run(transport=...)``，一个中间件都不挂（与接入前逐字节一致）；配置后每个
http 请求必须带 ``Authorization: Bearer <token>``，恒时比较，否则在进入 MCP 处理
之前就 401。stdio 不经过这里。

**豁免面是三条，不是两条**——本线的直连探针与运维看板都不经网关：

- ``/health``：Deployment 的探针（``harden-erp-operations`` 4.1），进程僵死的唯一判据；
- ``/metrics``：``ops-console`` 的 ``ERP_METRICS_TARGETS`` 直接抓 ClusterIP（Panel 里
  写死 ``http://10.43.146.146/metrics``），门掉它等于把 ERP 看板打成空白；
- ``/healthz``：本线没有这条路由，但家族里其他线有，一并豁免免得后来人加健康面时踩坑。

这三条都只回状态/指标，不是数据面；**数据面 ``/mcp`` 不在豁免里**。

形状与 ``fd-zhesi-reference`` / ``fd-quant`` / ``fd-text-core`` 的门逐行同族，运维只需
记一个变量名（``MCP_BEARER_TOKEN``）。它给的是「摘掉壹座后本线仍能被合法消费」这条
能力，而**不是**客户级身份：一把钥匙全客户共用，谁在用由外层（网关/registry）记录。
每客户一把钥匙（词元轨 ``ciyuan-sk``）需要钥匙库 + 吊销面，见 manifest 里那条轨的
``migration_task``。
"""

from __future__ import annotations

import hmac
import os

from . import wellknown

ENV_TOKEN = "MCP_BEARER_TOKEN"

#: 免门路径：探针、运维指标、家族约定的 /healthz，以及**线自描述端点**（规格要求它
#: 匿名可读，否则第三方无法发现本线）。数据面 /mcp 刻意不在其中。
EXEMPT_PATHS = ("/health", "/healthz", "/metrics", wellknown.WELL_KNOWN_PATH)


def configured_token() -> str:
    """每请求重读：改旗/轮换不需要重启进程。"""
    return os.environ.get(ENV_TOKEN, "").strip()


class BearerAuthMiddleware:
    """ASGI 中间件：配了 token 就要求匹配的 bearer，未配则不设门。

    只拦 ``http`` scope——lifespan 必须放过去，否则应用起不来；豁免面按
    ``EXEMPT_PATHS``，否则探针会被 401 打死、看板会静默变空。
    """

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    def __call__(self, scope, receive, send):
        token = configured_token()
        if scope["type"] != "http" or not token:
            return self.asgi_app(scope, receive, send)
        if (scope.get("path") or "") in EXEMPT_PATHS:
            return self.asgi_app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")
        expected = ("Bearer " + token).encode("utf-8", "surrogateescape")
        if hmac.compare_digest(provided, expected):
            return self.asgi_app(scope, receive, send)

        async def _reject(receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})

        return _reject(receive, send)
