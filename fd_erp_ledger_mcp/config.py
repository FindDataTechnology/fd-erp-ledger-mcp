"""惰性配置：零配置可启动，缺数据源时调用点如实报 UPSTREAM_UNAVAILABLE。

BYOK 纪律（erp-board / crm-board 契约）：外部系统凭证只经环境变量注入，
零落库、零日志。本模块不读取、也不缓存任何凭证明文。
"""
from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


def _tz(name: str, default: str) -> str:
    """账面时区：显式配置，非法即在启动期拒绝。

    「今天」是个与地点有关的词——UTC 的 23:30 在上海已是次日。业务日期的默认值若取自
    进程本地时区，同一套部署换台机器就会把单据归到不同期间，而这种偏移不报错，
    只让报表和客户账面对不上。所以时区必须显式，且错了要当场说（不要退回默认值装没事）。
    """
    raw = _env(name, default) or default
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"{name}={raw!r} 不是合法时区名（例：Asia/Shanghai / UTC）") from None
    return raw


#: 账面时区默认值（与迁移 0014 的回填口径一致）。
DEFAULT_BOOK_TIMEZONE = "Asia/Shanghai"


def resolve_book_timezone(settings: Any) -> str:
    """取账面时区，对「精简版 Settings」宽容但**不静默**。

    内核 `Settings` 自带 `book_timezone`；嵌入方（API 层、脚本）常传一个只为内核函数
    提供 `database_url/base_currency/tenant_id` 的同形对象。那种对象没有这个字段，
    此时用默认值——并把用到的时区回显在关账结果里（`report.bookTimezone`），
    让「实际按哪个时区算的今天」可核对，而不是靠猜。
    """
    raw = getattr(settings, "book_timezone", None)
    return str(raw) if raw else DEFAULT_BOOK_TIMEZONE


def book_today(settings: Any) -> datetime.date:
    """账面时区的「今天」——业务日期缺省值（跨 Settings 类型的唯一算法出处）。"""
    return datetime.datetime.now(ZoneInfo(resolve_book_timezone(settings))).date()


def book_period(settings: Any, day: datetime.date | None = None) -> str:
    """业务日期 → 会计期间（`YYYY-MM`）。纯 DATE 运算，与任何会话时区无关。"""
    return (day or book_today(settings)).strftime("%Y-%m")


@dataclass(frozen=True)
class Settings:
    """一个 cell 对应恰好一个 tenant（wire-cell-tenancy 的进程/数据根边界）。

    内核另加「租户列 + 查询闸」做纵深防御（本 change design.md 决策，见 README）。
    """

    #: DSN 含口令：repr=False 让异常栈 / pprint / REPL 也不出明文。
    #: 日志层另有 observability 的键名与形状打码，但那只覆盖走日志的路径。
    database_url: str | None = field(default=None, repr=False)
    tenant_id: str = "default"
    base_currency: str = "CNY"
    #: 占用失效时点的上界（天）。受理方声明的 expires_at 不得超过它，否则视为
    #: 用超长时点规避对账。由部署配置给出，不硬编码在业务层。
    credit_reservation_max_days: int = 90
    #: 非本位币费用折算默认取用的汇率出处（fx_facts.source）。规则 config 里可用
    #: `fx_source` 覆盖；两边都没有出处 → 计费拒绝（FX_UNSOURCED），不猜默认汇率。
    #: 默认 ecb-exr：fx_facts 的做种来源（`seed-fx` 的 ecb-exr 观测流）。
    fx_source: str = "ecb-exr"
    #: 账面时区（change: add-period-close）：业务日期默认值、期间边界都按它解释。
    #: 显式配置而非取进程本地时区——否则换台机器就把单据归到别的期间。
    book_timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        # 直接构造（测试/嵌入方）也要过同一道校验：非法时区不能等到第一次算「今天」才炸。
        try:
            ZoneInfo(self.book_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"book_timezone={self.book_timezone!r} 不是合法时区名"
                "（例：Asia/Shanghai / UTC）") from None

    @classmethod
    def from_env(cls) -> "Settings":
        raw_max = _env("ERP_CREDIT_RESERVATION_MAX_DAYS", "90")
        try:
            max_days = max(1, int(raw_max or "90"))
        except ValueError:
            max_days = 90
        return cls(
            database_url=_env("ERP_LEDGER_DATABASE_URL"),
            tenant_id=_env("ERP_TENANT_ID", "default") or "default",
            base_currency=_env("ERP_BASE_CURRENCY", "CNY") or "CNY",
            credit_reservation_max_days=max_days,
            fx_source=_env("ERP_FX_SOURCE", "ecb-exr") or "ecb-exr",
            book_timezone=_tz("ERP_BOOK_TIMEZONE", "Asia/Shanghai"),
        )

    # ---- 账面日期工具（业务日期的唯一算法出处） ----

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.book_timezone)

    def today(self) -> datetime.date:
        """账面时区的「今天」——业务日期缺省值。"""
        return book_today(self)

    def period_of(self, day: datetime.date) -> str:
        """业务日期 → 会计期间（`YYYY-MM`）。纯 DATE 运算，与任何会话时区无关。"""
        return day.strftime("%Y-%m")
