"""`schema_for()` 的六臂（openspec: erp-tenant-schema-isolation 1.1）。

离线用例：不碰数据库（标 `no_db`，否则会被 conftest 的真库夹具整体 skip 掉——
那等于没验，见 conftest `_reset_db` 注释里 2026-11-08 那次 2 skipped 的教训）。
"""
from __future__ import annotations

import pytest

from fd_erp_ledger_mcp.errors import ErpError
from fd_erp_ledger_mcp.tenancy import MAX_SLUG_LEN, schema_for

pytestmark = pytest.mark.no_db


def test_default_tenant_maps_to_public():
    """臂 1：租户 0 → `public`（这条映射就是生产零迁移）。"""
    assert schema_for("default") == "public"


def test_valid_slug_gets_tenant_prefix():
    """臂 2：合法 slug → `tenant_<slug>`。"""
    assert schema_for("acme") == "tenant_acme"
    assert schema_for("acme_2026") == "tenant_acme_2026"


@pytest.mark.parametrize(
    "tenant",
    ["客户甲", "acme-01"],
    ids=["chinese", "hyphen"],
)
def test_illegal_chars_are_rejected_by_name(tenant: str):
    """臂 3/4：中文与连字符 ⇒ INVALID_ARGUMENT，且**指名**违规字符。"""
    with pytest.raises(ErpError) as ei:
        schema_for(tenant)
    err = ei.value
    assert err.code == "INVALID_ARGUMENT"
    illegal = err.detail.get("illegal_chars") or []
    # 点名的必须是真正违规的那些字符，不能是「统一说一句不合法」
    for ch in illegal:
        assert ch in tenant
    assert tenant not in (err.message or "")  # 整串不该被当成字符清单
    assert "非法字符" in err.message


def test_overlong_slug_is_rejected_with_lengths():
    """臂 5：超长 ⇒ INVALID_ARGUMENT，detail 里带上实际长度与上限。"""
    too_long = "a" * (MAX_SLUG_LEN + 1)
    with pytest.raises(ErpError) as ei:
        schema_for(too_long)
    err = ei.value
    assert err.code == "INVALID_ARGUMENT"
    assert err.detail["length"] == MAX_SLUG_LEN + 1
    assert err.detail["max_len"] == MAX_SLUG_LEN
    # 边界另一侧必须是合法的：只钉超长一侧，把 `>` 写成 `>=` 也测不出来
    assert schema_for("a" * MAX_SLUG_LEN) == "tenant_" + "a" * MAX_SLUG_LEN


def test_empty_slug_is_rejected():
    """臂 6：空串 ⇒ INVALID_ARGUMENT（不能静默变成 `tenant_`）。"""
    with pytest.raises(ErpError) as ei:
        schema_for("")
    assert ei.value.code == "INVALID_ARGUMENT"
    assert "不能为空" in ei.value.message
