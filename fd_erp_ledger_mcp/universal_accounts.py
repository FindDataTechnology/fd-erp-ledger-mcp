"""通用科目最小集（erp-vertical-templates 任务 1.1）。

真源 = migrations/0002_universal_accounts.sql（universal_accounts 预设表）。
本模块只暴露逐租户种子与只读查询口径：
- seed_universal_accounts：幂等写 accounts（INSERT .. ON CONFLICT DO NOTHING）。
- list_universal_accounts：读预设表（科目底座是什么，供 selfcheck/文档核对）。

行业科目 MUST NOT 注入内核：行业口径一律走科目映射 pack（呈现层）。
"""
from __future__ import annotations

from typing import Any

PRESET_VERSION = "v1"


def seed_universal_accounts(conn: Any, tenant_id: str) -> dict[str, Any]:
    """把通用科目最小集种入租户 accounts（幂等；返回本次实际新增行数）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT erp_seed_universal_accounts(%s)", (tenant_id,))
        seeded = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM accounts WHERE tenant_id=%s "
            "AND code IN (SELECT code FROM universal_accounts)",
            (tenant_id,),
        )
        present = cur.fetchone()[0]
    return {"tenant_id": tenant_id, "seeded": seeded, "universal_present": present,
            "preset_version": PRESET_VERSION}


def list_universal_accounts(conn: Any) -> list[dict[str, Any]]:
    """读通用科目预设（内核级，租户无关）。"""
    from .db import fetch_all
    rows = fetch_all(
        conn,
        "SELECT code, name, name_en, account_type, preset_version, note "
        "FROM universal_accounts ORDER BY code",
    )
    return rows
