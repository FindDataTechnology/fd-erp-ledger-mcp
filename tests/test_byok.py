"""BYOK 凭证纪律（3.2 / erp-board 执行面 BYOK）：只经环境变量注入，零落库零日志。"""
from __future__ import annotations

import pytest

from fd_erp_ledger_mcp.byok import credential_policy, redact, resolve_credential
from fd_erp_ledger_mcp.errors import ErpError


def test_resolve_reads_env(monkeypatch):
    monkeypatch.setenv("ERP_BYOK_PAYMENT", "s3cr3t-token")
    assert resolve_credential("payment") == "s3cr3t-token"
    assert resolve_credential("PAYMENT") == "s3cr3t-token"  # 大小写不敏感


def test_missing_returns_none(monkeypatch):
    monkeypatch.delenv("ERP_BYOK_ABSENT", raising=False)
    assert resolve_credential("absent") is None


def test_invalid_name_rejected():
    with pytest.raises(ErpError) as ei:
        resolve_credential("../etc/passwd")
    assert ei.value.code == "INVALID_ARGUMENT"


def test_redact_scrubs_secret():
    assert redact("token=abc123 in log", "abc123") == "token=<redacted> in log"


def test_policy_declares_no_persistence():
    policy = credential_policy()
    assert policy["persisted_to_db"] is False
    assert policy["written_to_logs"] is False
    assert policy["injection"] == "environment-variable-only"


def test_credential_never_written_to_db(settings):
    """验证用例（crm-board 契约）：注入凭证并跑对账，DB 中不出现明文。"""
    import os

    import psycopg

    from fd_erp_ledger_mcp.recon.engine import enqueue, wait_for_task

    os.environ["ERP_BYOK_LEAKCHECK"] = "LEAKCANARY-9f3a"
    try:
        # 用一个会读取凭证的 api 源（不可达 → 失败也绝不落库明文）
        task = enqueue(
            settings, "acme",
            {"type": "api", "url": "http://127.0.0.1:1/nope",
             "credential_name": "LEAKCHECK"},
            {"type": "inline", "records": []},
            None,
        )
        wait_for_task(settings, "acme", task["task_id"], timeout=5.0)
        conn = psycopg.connect(settings.database_url, autocommit=True)
        try:
            leaked = 0
            for table, col in [
                ("recon_reports", "result::text"), ("recon_reports", "error"),
                ("audit", "detail::text"), ("proposals", "payload::text"),
            ]:
                row = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE {col} LIKE %s",
                    ("%LEAKCANARY-9f3a%",),
                ).fetchone()
                leaked += row[0]
            assert leaked == 0, "BYOK 凭证明文出现在数据库"
        finally:
            conn.close()
    finally:
        os.environ.pop("ERP_BYOK_LEAKCHECK", None)
