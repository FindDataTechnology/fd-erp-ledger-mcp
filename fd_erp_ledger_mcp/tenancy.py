"""租户查询闸（erp-ledger-kernel / erp-board）。

设计裁定（见 openspec/changes/add-erp-domain/design.md）：内核采用「逻辑隔离
（租户列 + 查询闸）」起步；wire-cell-tenancy 的「进程+数据根物理隔离、引擎
不感知 tenant」是柏讯 DAAS 引擎的约束，不是本新服务的约束。本服务每条表都带
tenant_id，所有查询强制带 tenant 过滤；物理隔离诉求由 appliance 私有化档位消化。

每个 cell 由 ERP_TENANT_ID 绑定唯一租户；任何请求携带别的 tenant_id 即判跨租户，
拒绝并写审计痕（erp-ledger-kernel「跨租户查询被拒」场景）。
"""
from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .errors import ErpError


def audit(conn: Any, tenant_id: str, action: str, *, target: str | None = None,
          actor: str = "system", detail: dict[str, Any] | None = None) -> None:
    """写审计痕。用独立事务，确保即使主事务回滚（如不平分录）留痕仍在。"""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit(tenant_id, actor, action, target, detail) "
                "VALUES (%s,%s,%s,%s,%s::jsonb)",
                (tenant_id, actor, action, target,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )


def resolve_tenant(conn: Any, settings: Settings, requested: str | None) -> str:
    """查询闸：返回本 cell 绑定的 tenant；跨租户请求拒绝 + 审计留痕。"""
    own = settings.tenant_id
    if requested is None or requested == own:
        return own
    # 跨租户：先留痕（独立事务），再拒绝。响应中绝不出现对方数据。
    try:
        audit(conn, own, "tenant_denied", actor=requested,
              target="cross_tenant_query",
              detail={"requested_tenant": requested, "cell_tenant": own})
    except Exception:
        pass  # 留痕失败也不能放行；拒绝本身仍成立
    raise ErpError(
        "TENANT_DENIED",
        f"跨租户访问被拒绝：本 cell 仅服务租户 {own}",
        detail={"requested_tenant": requested, "cell_tenant": own},
    )


def tenant_filter(tenant_id: str) -> str:
    """所有业务查询必须带此过滤（纵深防御）。"""
    return "tenant_id = %s", (tenant_id,)
