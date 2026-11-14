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

from . import line_token, wellknown

ENV_TOKEN = "MCP_BEARER_TOKEN"

#: 免门路径：探针、运维指标、家族约定的 /healthz，以及**线自描述端点**（规格要求它
#: 匿名可读，否则第三方无法发现本线）。数据面 /mcp 刻意不在其中。
EXEMPT_PATHS = ("/health", "/healthz", "/metrics", wellknown.WELL_KNOWN_PATH)

#: 校验通过的门 K 身份放这里（ASGI scope 键）。命名带线名，免得同一进程里
#: 两条线的身份串味。**不做成员级判定**：本仓没有客户表，租户范围由平台经
#: `X-ERP-*` 注入后由业务代码判（见 line_token 模块头注）。
SCOPE_IDENTITY = "erp.line_identity"


def configured_token() -> str:
    """每请求重读：改旗/轮换不需要重启进程。"""
    return os.environ.get(ENV_TOKEN, "").strip()


def gate_enabled() -> bool:
    """门存在与否 = 「共享钥匙」或「词元轨」任一被配置。

    两者都空时**一个中间件都不挂**，与接入前逐字节一致（`cli.serve()` 用这个
    判断而不是 `configured_token()`，否则只开词元轨的部署会没有门）。
    """
    return bool(configured_token()) or line_token.settings_from_env().configured


class BearerAuthMiddleware:
    """ASGI 中间件：配了共享钥匙或词元轨就设门，两者都未配则不设门。

    只拦 ``http`` scope——lifespan 必须放过去，否则应用起不来；豁免面按
    ``EXEMPT_PATHS``，否则探针会被 401 打死、看板会静默变空。

    两条轨道，**顺序是刻意的**：先做共享钥匙的恒时比较（便宜、无解析），不匹配才
    去验词元票（要验签）。反过来会让每一发共享钥匙都白付一次 JWT 解析，而共享钥匙
    是本线最常用的那条路（平台代调也是它）。
    """

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return self.asgi_app(scope, receive, send)
        shared = configured_token()
        line_settings = line_token.settings_from_env()
        if not shared and not line_settings.configured:
            return self.asgi_app(scope, receive, send)
        if (scope.get("path") or "") in EXEMPT_PATHS:
            return self.asgi_app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")

        # ① 线自有共享钥匙（回退轨）：恒时比较。
        if shared:
            expected = ("Bearer " + shared).encode("utf-8", "surrogateescape")
            if hmac.compare_digest(provided, expected):
                return self.asgi_app(scope, receive, send)

        # ② 门 K：网关换发来的短命票，离线验签。失败**不解释**（与家族同口径：
        #    只说"令牌不对"，不告诉调用方是签名、iss、aud 还是 source 没过）。
        if line_settings.configured:
            raw = provided.decode("utf-8", "surrogateescape")
            bearer = raw[len("Bearer ") :] if raw.startswith("Bearer ") else ""
            if bearer.strip():
                try:
                    ident = line_token.verify_line_token(bearer.strip(), settings=line_settings)
                except line_token.LineTokenRejected:
                    ident = None
                if ident is not None:
                    # 只挂身份，不在这里判权限：读/写各自由路由按 `can_write` 决定。
                    scope[SCOPE_IDENTITY] = ident
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

