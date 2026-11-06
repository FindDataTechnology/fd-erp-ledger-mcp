"""两源流水接入抽象（erp-ledger-kernel 对账引擎只读滩头 / 任务 4.1）。

- SourceAdapter 接口：read() -> list[SourceRecord]，只读，零外部写入。
- 本 change 交付通用 CSV 抽取器 + 通用 API 抽取器（BYOK 经环境变量注入）；
  平台专用连接器归 add-erp-vertical-templates（模板 change）。
- 实证对账阶段对外部系统零写入：适配器只有 read，没有 write/push 路径。
"""
from __future__ import annotations

import csv
import io
import json
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from ..byok import resolve_credential
from ..errors import ErpError


class SourceRecord:
    """标准化流水记录。stable_id 为幂等键（evidence 门 sha1 纪律）。"""

    __slots__ = ("stable_id", "amount", "currency", "status", "occurred_at", "source_ref", "raw")

    def __init__(self, stable_id: str, amount: Decimal, currency: str,
                 status: str, occurred_at: str | None, source_ref: str,
                 raw: dict[str, Any]):
        self.stable_id = stable_id
        self.amount = amount
        self.currency = currency
        self.status = status
        self.occurred_at = occurred_at
        self.source_ref = source_ref
        self.raw = raw

    def as_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id, "amount": str(self.amount),
            "currency": self.currency, "status": self.status,
            "occurred_at": self.occurred_at, "source_ref": self.source_ref,
            "raw": self.raw,
        }


class SourceAdapter(ABC):
    """只读源适配器接口。实现体 MUST NOT 暴露任何写入外部系统的方法。

    iter_raw() 逐条产出原始记录，供长任务做「逐条成败 + 仅失败条目重试」；
    read() 是严格批量视图（任一条非法即整体报错）。
    """

    name = "abstract"

    @abstractmethod
    def iter_raw(self) -> list[dict[str, Any]]:  # pragma: no cover - interface
        ...

    @abstractmethod
    def side(self) -> str:  # pragma: no cover - interface
        ...

    def read(self) -> list[SourceRecord]:
        return [_to_record(r, self.side(), i) for i, r in enumerate(self.iter_raw())]

    def read_tolerant(self) -> tuple[list[SourceRecord], list[dict[str, Any]]]:
        """逐条转换：坏记录记为失败条目（含原因），不拖垮整批。"""
        records: list[SourceRecord] = []
        failures: list[dict[str, Any]] = []
        for i, raw in enumerate(self.iter_raw()):
            try:
                records.append(_to_record(raw, self.side(), i))
            except ErpError as exc:
                failures.append({
                    "item_key": str(raw.get("stable_id") or raw.get("id") or f"{self.side()}:{i}"),
                    "reason": exc.code,
                    "message": exc.message,
                    "raw": raw,
                })
        return records, failures

    def describe(self) -> dict[str, Any]:
        return {"adapter": self.name, "writes_external": False}


def _to_record(item: dict[str, Any], side: str, index: int) -> SourceRecord:
    stable_id = str(item.get("stable_id") or item.get("id") or "").strip()
    if not stable_id:
        # 无 stable_id 的记录不能参与对齐：记成失败条目，供重试/解释
        raise ErpError("INVALID_ARGUMENT", f"{side} 第 {index} 条流水缺 stable_id")
    try:
        amount = Decimal(str(item.get("amount")))
    except Exception as exc:
        raise ErpError("INVALID_ARGUMENT", f"{side} 第 {index} 条 amount 非法") from exc
    return SourceRecord(
        stable_id=stable_id,
        amount=amount,
        currency=str(item.get("currency") or "CNY"),
        status=str(item.get("status") or "posted"),
        occurred_at=item.get("occurred_at"),
        source_ref=str(item.get("source_ref") or f"{side}:{index}"),
        raw=dict(item),
    )


class InlineAdapter(SourceAdapter):
    """内联记录（e2e / 合成两源用），records 为 dict 列表。"""

    name = "inline"

    def __init__(self, records: list[dict[str, Any]], side: str = "left"):
        self._records = records
        self._side = side

    def side(self) -> str:
        return self._side

    def iter_raw(self) -> list[dict[str, Any]]:
        return list(self._records)


class CsvAdapter(SourceAdapter):
    """通用 CSV 抽取器：csv_text 为字符串或文件路径（file: 前缀）。"""

    name = "csv"

    def __init__(self, csv_text: str, side: str = "left"):
        self._csv_text = csv_text
        self._side = side

    def side(self) -> str:
        return self._side

    def iter_raw(self) -> list[dict[str, Any]]:
        text = self._csv_text
        if text.startswith("file:"):
            try:
                with open(text[5:], encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as exc:
                raise ErpError("UPSTREAM_UNAVAILABLE", f"CSV 文件不可读：{exc}") from exc
        return [dict(r) for r in csv.DictReader(io.StringIO(text))]


class JsonApiAdapter(SourceAdapter):
    """通用只读 API 抽取器：GET 一段 JSON（records 列表或裸数组）。

    凭证（若有）只经 ERP_BYOK_<NAME> 环境变量注入，零落库零日志。
    """

    name = "api"

    def __init__(self, url: str, side: str = "left", credential_name: str | None = None,
                 records_key: str = "records"):
        self._url = url
        self._side = side
        self._credential_name = credential_name
        self._records_key = records_key

    def side(self) -> str:
        return self._side

    def iter_raw(self) -> list[dict[str, Any]]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ErpError("UPSTREAM_UNAVAILABLE", "缺少 httpx 依赖") from exc
        headers: dict[str, str] = {}
        if self._credential_name:
            token = resolve_credential(self._credential_name)
            if not token:
                raise ErpError("UPSTREAM_UNAVAILABLE",
                               f"BYOK 凭证未注入：ERP_BYOK_{self._credential_name.upper()}")
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = httpx.get(self._url, headers=headers, timeout=10.0)
            resp.raise_for_status()
            body = resp.json()
        except ErpError:
            raise
        except Exception as exc:
            raise ErpError("UPSTREAM_UNAVAILABLE", f"上游 API 不可用：{exc}") from exc
        items = body.get(self._records_key, []) if isinstance(body, dict) else body
        if not isinstance(items, list):
            raise ErpError("UPSTREAM_UNAVAILABLE", "上游 API 返回的 records 不是数组")
        return [dict(r) if isinstance(r, dict) else {"stable_id": str(r)} for r in items]


def build_adapter(spec: dict[str, Any], side: str) -> SourceAdapter:
    """从声明构造适配器；平台连接器归模板 change，此处仅通用三型。"""
    if not isinstance(spec, dict):
        raise ErpError("INVALID_ARGUMENT", f"{side} source 须为对象")
    kind = str(spec.get("type") or "inline")
    try:
        if kind == "inline":
            return InlineAdapter(list(spec.get("records") or []), side)
        if kind == "csv":
            return CsvAdapter(str(spec.get("csv") or spec.get("path") or ""), side)
        if kind == "api":
            return JsonApiAdapter(str(spec.get("url") or ""), side,
                                  spec.get("credential_name"),
                                  str(spec.get("records_key") or "records"))
    except ErpError:
        raise
    except Exception as exc:
        raise ErpError("INVALID_ARGUMENT", f"{side} source 配置非法：{exc}") from exc
    raise ErpError("NOT_WIRED",
                   f"暂不支持的源适配器类型 {kind}（平台连接器归 add-erp-vertical-templates）",
                   detail={"type": kind, "side": side})
