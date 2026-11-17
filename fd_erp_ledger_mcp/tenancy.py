"""租户查询闸（erp-ledger-kernel / erp-board）+ 租户 → schema 映射。

设计演进（openspec/changes/erp-tenant-schema-isolation）：隔离边界已从「租户列 +
查询闸」升到**数据库 schema**——每租户一个 schema，`search_path` 是边界，`tenant_id`
列降级为纵深防御。原因：逻辑隔离的强度取决于每一处查询都记得带条件，漏一个 WHERE
就串数据；而 schema 隔离下代码漏了条件也读不到别人的行。附带收益是私有化交付与共享
部署同构（同一份 DDL、同一份代码，`pg_dump -n` 即可搬迁）。

每个 cell 由 ERP_TENANT_ID 绑定唯一租户；任何请求携带别的 tenant_id 即判跨租户，
拒绝并写审计痕（erp-ledger-kernel「跨租户查询被拒」场景）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .config import Settings
from .errors import ErpError

#: 租户 0 的名字。生产既有数据都在 `public`，所以它必须映射到 `public`——
#: 这条映射就是「零迁移」：`ERP_TENANT_ID` 不设（或设成它）时 search_path 不变。
DEFAULT_TENANT = "default"
PUBLIC_SCHEMA = "public"

#: 开通方指定 slug，只允许小写字母/数字/下划线，长度 ≤32。
#: **不接受**中文与其它字符：schema 名要拼进 `search_path`（DDL 位置，不能用参数
#: 占位符），白名单校验因此同时是注入防线——放进来一个引号或分号就等于放进来一段 SQL。
SLUG_PATTERN = re.compile(r"^[a-z0-9_]+$")
MAX_SLUG_LEN = 32
TENANT_SCHEMA_PREFIX = "tenant_"


def schema_for(tenant_id: str) -> str:
    """租户 id → schema 名：`default` → `public`，其余 → `tenant_<slug>`。

    slug 不合白名单（`[a-z0-9_]`、长度 ≤32、非空）即抛 `INVALID_ARGUMENT` 并指名
    违规字符——静默改成 `tenant_` 或静默截断都会让两个租户映射到同一个 schema，
    那是「隔离失效」最坏的一种形态：不报错、数据混在一起。
    """
    raw = "" if tenant_id is None else str(tenant_id)
    if raw == DEFAULT_TENANT:
        return PUBLIC_SCHEMA
    if not raw:
        raise ErpError(
            "INVALID_ARGUMENT",
            "租户 id 不能为空",
            hint="租户 id 取 [a-z0-9_]，长度 1-32；租户 0 的名字是 default",
            detail={"tenant_id": raw, "max_len": MAX_SLUG_LEN},
        )
    if len(raw) > MAX_SLUG_LEN:
        raise ErpError(
            "INVALID_ARGUMENT",
            f"租户 id 超长：{len(raw)} 字符（上限 {MAX_SLUG_LEN}）",
            detail={"tenant_id": raw, "length": len(raw), "max_len": MAX_SLUG_LEN},
        )
    bad = sorted({ch for ch in raw if not SLUG_PATTERN.fullmatch(ch)})
    if bad:
        raise ErpError(
            "INVALID_ARGUMENT",
            "租户 id 含非法字符：" + "、".join(repr(ch) for ch in bad),
            hint="只允许小写字母、数字与下划线（中文与连字符会被拒绝，避免 schema 名转义问题）",
            detail={"tenant_id": raw, "illegal_chars": bad},
        )
    return f"{TENANT_SCHEMA_PREFIX}{raw}"


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
