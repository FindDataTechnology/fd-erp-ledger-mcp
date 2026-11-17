"""CLI：serve / migrate / seed-fx / check-schema / version。"""
from __future__ import annotations

import json

import click

from . import __version__
from .config import Settings
from .db import assert_schema_version, get_pool, mark_adopted, migrate
from .fx import seed_fx_facts


@click.group()
@click.version_option(__version__, prog_name="fd-erp-ledger-mcp")
def cli() -> None:
    """ERP 账本内核 MCP 服务。"""


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, type=int, show_default=True)
@click.option("--transport", type=click.Choice(["http", "sse", "stdio"]),
              default="http", show_default=True)
@click.option("--skip-schema-check", is_flag=True,
              help="跳过启动 schema 版本闸（仅本地开发用）")
def serve(host: str, port: int, transport: str, skip_schema_check: bool) -> None:
    from .server import create_server
    settings = Settings.from_env()
    if not skip_schema_check and settings.database_url:
        # db-schema-lifecycle：启动前比对版本，落后即 fail-fast
        assert_schema_version(settings)
        # optimize-erp-solution 6.5：遗留对账任务续跑或显式重排（不再静默留 interrupted）。
        # 处置结果同时写进程内记录（/health 的 recon_startup）与结构化日志。
        from .recon.engine import resume_orphans
        resume_orphans(settings)
    server = create_server(settings)
    if transport == "stdio":
        server.run(transport="stdio")
        return

    # 线自描述端点（change converge-line-identity-and-keys 1.4；规格第 23 行要求两处运行时
    # 投影——`/.well-known/line-manifest.json` 与注册表 `metadata.identity`——本线此前
    # 只有后者）。必须在 `http_app()` **之前**挂：它走 FastMCP 的 custom_route，路由属于
    # 应用本身，配/未配令牌两个分支都要能命中。
    from . import wellknown

    wellknown.register(server)

    # change converge-line-identity-and-keys 8.5：共享令牌**或**词元轨任一配了就挂门，
    # 两者都未配则与接入前逐字节一致（`middleware=None` 时 http_app 不装任何中间件）。
    # `/health` 与 `/metrics` 在 auth.EXEMPT_PATHS 里永远豁免——前者是 liveness 的唯一
    # 判据，后者是 ops-console 直连 ClusterIP 抓取的看板数据源，两者都不经网关、不带钥匙。
    from . import auth

    middleware = None
    if auth.gate_enabled():
        from starlette.middleware import Middleware

        middleware = [Middleware(auth.BearerAuthMiddleware)]

    # 路径与 `server.run(transport=...)` 的默认值对齐：http 走 /mcp，sse 走 /sse。
    app = server.http_app(
        path="/sse" if transport == "sse" else "/mcp",
        middleware=middleware,
        transport="sse" if transport == "sse" else "http",
    )

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


@cli.command()
def migrate_cmd() -> None:
    """迁移阶段：一次性、可重放、幂等。运行时绝不发 DDL。"""
    settings = Settings.from_env()
    result = migrate(settings)
    click.echo(json.dumps(result, ensure_ascii=False))


cli.add_command(migrate_cmd, name="migrate")


@cli.command("check-schema")
def check_schema() -> None:
    from .db import REQUIRED_SCHEMA_VERSION
    settings = Settings.from_env()
    assert_schema_version(settings)
    click.echo(json.dumps({"ok": True, "required": REQUIRED_SCHEMA_VERSION}, ensure_ascii=False))


@cli.command("mark-adopted")
@click.option("--version", "version", default="0001_baseline", show_default=True)
def mark_adopted_cmd(version: str) -> None:
    """既有库 adopt：只登记版本，不执行任何 DDL。"""
    settings = Settings.from_env()
    click.echo(json.dumps(mark_adopted(settings, version), ensure_ascii=False))


@cli.command("seed-fx")
@click.argument("observations_file", type=click.Path(exists=True, dir_okay=False))
def seed_fx(observations_file: str) -> None:
    """从 ecb-exr 形状观测流 JSON 文件做种 fx_facts（无出处拒绝）。"""
    settings = Settings.from_env()
    with open(observations_file, encoding="utf-8") as fh:
        body = json.load(fh)
    observations = body.get("observations", body) if isinstance(body, dict) else body
    conn = get_pool(settings).getconn()
    try:
        result = seed_fx_facts(conn, observations, settings=settings)
    finally:
        get_pool(settings).putconn(conn)
    click.echo(json.dumps(result, ensure_ascii=False))


@cli.command("seed-accounts")
@click.option("--tenant", default=None, show_default=True,
              help="目标租户（缺省取 ERP_TENANT_ID）")
@click.option("--all-tenants", is_flag=True,
              help="为 accounts 表中现存的每个租户都做一次幂等种子")
def seed_accounts(tenant: str | None, all_tenants: bool) -> None:
    """种入通用科目最小集（erp-vertical-templates 任务 1.1；幂等可重放）。"""
    from .universal_accounts import seed_universal_accounts
    settings = Settings.from_env()
    conn = get_pool(settings).getconn()
    try:
        if all_tenants:
            from .db import fetch_all
            tenants = [r["tenant_id"] for r in fetch_all(
                conn, "SELECT DISTINCT tenant_id FROM accounts ORDER BY tenant_id")]
            results = [seed_universal_accounts(conn, t) for t in tenants]
            result = {"mode": "all_tenants", "tenants": tenants, "results": results}
        else:
            result = seed_universal_accounts(conn, tenant or settings.tenant_id)
    finally:
        get_pool(settings).putconn(conn)
    click.echo(json.dumps(result, ensure_ascii=False))


@cli.command("set-credit-limit")
@click.option("--tenant", default=None, show_default=False,
              help="目标租户（缺省取 ERP_TENANT_ID）")
@click.option("--customer", required=True, help="客户标识")
@click.option("--limit-amount", required=True, help="额度上限（数字，原样传给 Decimal）")
@click.option("--currency", default="CNY", show_default=True)
@click.option("--source-ref", default=None, help="额度事实出处（额度政策/审批单号）")
def set_credit_limit_cmd(tenant: str | None, customer: str, limit_amount: str,
                         currency: str, source_ref: str | None) -> None:
    """登记/调增客户信用额度上限（运维动作；额度事实带出处）。

    MCP 工具面按契约固定为 reserve/release/query 三件，额度登记走本运维命令。
    """
    from .credit import set_credit_limit
    settings = Settings.from_env()
    conn = get_pool(settings).getconn()
    try:
        result = set_credit_limit(conn, tenant_id=tenant or settings.tenant_id,
                                  customer_id=customer, limit_amount=limit_amount,
                                  currency=currency, source_ref=source_ref)
    finally:
        get_pool(settings).putconn(conn)
    click.echo(json.dumps(result, ensure_ascii=False))


@cli.command("register-fee-item")
@click.option("--tenant", default=None, show_default=False,
              help="目标租户（缺省取 ERP_TENANT_ID）")
@click.option("--fee-item", required=True, help="收费项（= 规则的 config.fee_item）")
@click.option("--event-kind", required=True, help="触发该收费项的业务事件类型")
@click.option("--source-ref", default=None, help="登记出处（价目表/合同/审批单号）")
@click.option("--status", default="in_use", type=click.Choice(["in_use", "retired"]),
              show_default=True, help="in_use = 在用（进入覆盖差集）；retired = 停用")
@click.option("--by", "registered_by", default="ops", show_default=True)
def register_fee_item_cmd(tenant: str | None, fee_item: str, event_kind: str,
                          source_ref: str | None, status: str, registered_by: str) -> None:
    """登记/停用收费项（optimize-erp-solution 5.5）。

    MCP 工具面口径固定（登记不是业务动作，是运维目录维护），走本命令。
    登记在用的收费项一旦没有 active 规则，规则审批与事件/期间计费都会被拒并点名。
    """
    from .billing import register_fee_item
    settings = Settings.from_env()
    conn = get_pool(settings).getconn()
    try:
        result = register_fee_item(conn, tenant_id=tenant or settings.tenant_id,
                                   fee_item=fee_item, event_kind=event_kind,
                                   source_ref=source_ref, registered_by=registered_by,
                                   status=status)
    finally:
        get_pool(settings).putconn(conn)
    click.echo(json.dumps(result, ensure_ascii=False))


@cli.command("bill-period")
@click.option("--tenant", default=None, show_default=False,
              help="目标租户（缺省取 ERP_TENANT_ID）")
@click.option("--period", required=True,
              help="计费期间（幂等键；日终场景 = 结算日 YYYY-MM-DD，CST 口径）")
@click.option("--snapshot-file", required=True, type=click.Path(exists=True, dir_okay=False),
              help='日终快照 JSON：{"snapshots":[{"fee_item","as_of","measures","refs"}]}')
@click.option("--source-ref", default=None, help="来源引用（外部快照单号等）")
def bill_period_cmd(tenant: str | None, period: str, snapshot_file: str,
                    source_ref: str | None) -> None:
    """日终快照计费（外部排程打点入口；幂等，失败非零退出）。

    CronJob 契约见 README：CST 00:10 触发结算前一日；容器内须 TZ=Asia/Shanghai。
    同 period 重跑不产生重复费用（返回 duplicate=true），所以 Job 可安全重试。
    """
    from .billing import bill_period_snapshots
    settings = Settings.from_env()
    with open(snapshot_file, encoding="utf-8") as fh:
        body = json.load(fh)
    snapshots = body.get("snapshots") if isinstance(body, dict) else body
    conn = get_pool(settings).getconn()
    try:
        result = bill_period_snapshots(conn, settings=settings,
                                       tenant_id=tenant or settings.tenant_id,
                                       period=period, snapshots=snapshots,
                                       source_ref=source_ref)
    except Exception as exc:  # 让 CronJob 以非零退出暴露失败，而不是"跑完了但没做事"
        code = getattr(exc, "code", None)
        click.echo(json.dumps({"ok": False, "period": period,
                               "error": {"code": code, "message": str(exc)}},
                              ensure_ascii=False))
        raise SystemExit(1) from exc
    finally:
        get_pool(settings).putconn(conn)
    click.echo(json.dumps(result, ensure_ascii=False))


@cli.group("tenant")
def tenant_cmd() -> None:
    """租户 schema 运维（openspec: erp-tenant-schema-isolation 2.2-2.4）。

    租户的开通/删除/列举都是 **schema 级**操作：清单即权威，不另建租户注册表
    （design D6——再建一张表就多一份必然漂移的真相）。
    """


def _tenant_settings(tenant_id: str) -> Settings:
    """按指定租户派生 Settings。

    `Settings` 是从 `ERP_TENANT_ID` 读的（一进程一租户），而运维命令要操作**别的**
    租户，所以不能靠改 env——`dataclasses.replace` 派一份即可，池的缓存键带 schema，
    两个租户的池互不干扰。
    """
    import dataclasses

    return dataclasses.replace(Settings.from_env(), tenant_id=tenant_id)


@tenant_cmd.command("create")
@click.argument("tenant_id")
def tenant_create(tenant_id: str) -> None:
    """开通租户：建 schema + 全量重放迁移 + 载入最小科目表。

    `default` 是库内置的租户 0（`public`），开通它是 no-op——生产零变更是短路
    保证，不是「碰巧没执行到」。
    """
    from .universal_accounts import seed_universal_accounts
    from .db import create_schema
    from .tenancy import PUBLIC_SCHEMA, schema_for

    settings = _tenant_settings(tenant_id)
    schema = schema_for(tenant_id)
    if schema == PUBLIC_SCHEMA:
        click.echo(json.dumps({"ok": True, "tenant": tenant_id, "schema": schema,
                               "note": "租户 0 是库内置的 public，无需开通"},
                              ensure_ascii=False))
        return
    create_schema(settings)
    applied = migrate(settings)
    conn = get_pool(settings).getconn()
    try:
        accounts = seed_universal_accounts(conn, tenant_id)
    finally:
        get_pool(settings).putconn(conn)
    click.echo(json.dumps({"ok": True, "tenant": tenant_id, "schema": schema,
                           "migrate": applied, "accounts": accounts},
                          ensure_ascii=False, default=str))


@tenant_cmd.command("drop")
@click.argument("tenant_id")
@click.option("--yes", is_flag=True, help="确认不可逆删除（缺省只做 dry-run 并退出非零）")
def tenant_drop(tenant_id: str, yes: bool) -> None:
    """删除租户：DROP SCHEMA CASCADE + 审计留痕。

    拒绝删除 `default`：它是租户 0，误删等于把生产库抹了。这是硬拒，不是警告。
    """
    from .db import get_pool as _get_pool
    from .db import drop_schema
    from .tenancy import PUBLIC_SCHEMA, schema_for

    schema = schema_for(tenant_id)
    if schema == PUBLIC_SCHEMA:
        click.echo(json.dumps(
            {"ok": False, "error": {"code": "TENANT_ZERO_PROTECTED",
                                    "message": "default 是租户 0（public schema），拒绝删除"}},
            ensure_ascii=False), err=True)
        raise SystemExit(1)
    if not yes:
        click.echo(json.dumps(
            {"ok": False, "tenant": tenant_id, "schema": schema,
             "error": {"code": "CONFIRMATION_REQUIRED",
                       "message": "这是不可逆操作；确认请加 --yes"}},
            ensure_ascii=False), err=True)
        raise SystemExit(1)
    settings = _tenant_settings(tenant_id)
    drop_schema(settings)
    click.echo(json.dumps({"ok": True, "tenant": tenant_id, "schema": schema,
                           "dropped": True}, ensure_ascii=False))


@tenant_cmd.command("list")
def tenant_list() -> None:
    """列举租户：schema 清单即租户清单（public 恒在，代表租户 0）。"""
    from .db import list_tenant_schemas

    settings = Settings.from_env()
    click.echo(json.dumps(list_tenant_schemas(settings), ensure_ascii=False, default=str))


if __name__ == "__main__":
    cli()
