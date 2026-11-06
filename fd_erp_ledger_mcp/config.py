"""惰性配置：零配置可启动，缺数据源时调用点如实报 UPSTREAM_UNAVAILABLE。

BYOK 纪律（erp-board / crm-board 契约）：外部系统凭证只经环境变量注入，
零落库、零日志。本模块不读取、也不缓存任何凭证明文。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


@dataclass(frozen=True)
class Settings:
    """一个 cell 对应恰好一个 tenant（wire-cell-tenancy 的进程/数据根边界）。

    内核另加「租户列 + 查询闸」做纵深防御（本 change design.md 决策，见 README）。
    """

    database_url: str | None = None
    tenant_id: str = "default"
    base_currency: str = "CNY"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=_env("ERP_LEDGER_DATABASE_URL"),
            tenant_id=_env("ERP_TENANT_ID", "default") or "default",
            base_currency=_env("ERP_BASE_CURRENCY", "CNY") or "CNY",
        )
