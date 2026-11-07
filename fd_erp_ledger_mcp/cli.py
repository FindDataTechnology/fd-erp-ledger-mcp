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
        from .recon.engine import mark_interrupted
        mark_interrupted(settings)
    server = create_server(settings)
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport=transport, host=host, port=port)


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


if __name__ == "__main__":
    cli()
