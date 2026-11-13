"""线自描述端点 ``/.well-known/line-manifest.json``（change converge-line-identity-and-keys 1.4）。

规格（`openspec/changes/converge-line-identity-and-keys/specs/line-manifest/spec.md` 第 23 行）：
运行时投影 **SHALL 有两处** —— `/.well-known/line-manifest.json` 与注册表条目的
`metadata.identity`，且都与仓内源逐字段一致。本线此前只有注册表那一处（2026-11-13 实测：
公网该路径 401/36 B、线内根本没有这条路由），故端点是**补齐规格缺口**，不是新增能力。

三条判据都对着端点自己，不拿邻近信号冒充：

1. 200 的正文与**仓内真源** ``line-manifest.json`` **逐字节相同** —— 端点是投影，不是第二真源；
2. ``content-type`` 必须是 JSON：本线同族有些线带 SPA/网页壳，一个 200 的 HTML 是假绿；
3. ``Cache-Control`` 只在 200 上出现；404（清单缺失）与 500（存在但坏）不能混 ——
   404 会让调用方以为"本来就没发布"。

**豁免必须成对**：路径常量只在此处定义一次，``auth.EXEMPT_PATHS`` 引用它；只挂路由不加豁免，
配了令牌的线仍会对匿名发现请求回 401（本族此前踩过）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

WELL_KNOWN_PATH = "/.well-known/line-manifest.json"
ENV_MANIFEST_PATH = "LINE_MANIFEST_PATH"
CACHE_CONTROL = "public, max-age=300"
_CONTENT_TYPE = "application/json; charset=utf-8"


def manifest_candidates() -> list[Path]:
    """候选链：显式 env 一项即封顶；否则源码树 / CWD / 包内三档。

    镜像里 ``WORKDIR=/app`` 且 Dockerfile 把清单 COPY 到 ``/app/line-manifest.json``，
    所以 CWD 那一档命中；源码树里靠 ``parents[1]``（本文件在 ``<repo>/fd_erp_ledger_mcp/``）。
    """
    override = os.environ.get(ENV_MANIFEST_PATH)
    if override:
        return [Path(override)]
    here = Path(__file__).resolve()
    return [
        here.parents[1] / "line-manifest.json",
        Path.cwd() / "line-manifest.json",
        here.parent / "line-manifest.json",
    ]


def manifest_path() -> Path:
    """实际取用的清单路径；**逐档扫存在性**，都不存在时返回首选位置（让 404 能点名期望路径）。

    必须扫，不能只取首选档：镜像里是 **console script 启动**，``sys.path[0]`` 是
    ``/usr/local/bin``，``import fd_erp_ledger_mcp`` 命中 **site-packages 的那份副本**，
    于是 ``here.parents[1]`` 指向 site-packages（那里没有清单），而清单在
    ``WORKDIR=/app/line-manifest.json``。只取首选档 ⇒ 端点永久 404，且构建期的
    ``test -s`` 断言照样是绿的（2026-11-13 实测：pod 内 404/40 B，而 ``/app`` 下 4893 B 就在）。
    同族其余线路都是扫存在性版本，此处是对齐，不是新发明。
    """
    candidates = manifest_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def manifest_response() -> tuple[int, bytes, dict[str, str]]:
    """(status, body, headers)。缺失=404、不可读/坏 JSON=500、正常=200 原样字节。"""
    path = manifest_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return 404, b'{"detail":"line-manifest not published"}', {}
    except OSError as exc:  # 权限/IO：与"没有"分开报
        return (
            500,
            json.dumps({"detail": f"line-manifest unreadable: {type(exc).__name__}"}).encode(),
            {},
        )
    try:
        json.loads(raw)
    except ValueError:
        return 500, b'{"detail":"line-manifest is not valid JSON"}', {}
    return 200, raw, {"cache-control": CACHE_CONTROL}


def register(mcp) -> None:
    """挂在 FastMCP 上。**必须在 ``http_app()`` 之前调用**——路由属于应用本身。"""
    if not hasattr(mcp, "custom_route"):
        raise RuntimeError(
            "fastmcp 缺 custom_route：无法挂 " + WELL_KNOWN_PATH + "（升级依赖，别静默跳过）"
        )
    from starlette.responses import Response

    @mcp.custom_route(WELL_KNOWN_PATH, methods=["GET"])
    async def line_manifest(_request):  # noqa: ANN001 - fastmcp 传 Request
        status, body, headers = manifest_response()
        return Response(
            content=body, status_code=status, media_type=_CONTENT_TYPE, headers=headers
        )
