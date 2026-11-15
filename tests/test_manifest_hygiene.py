"""清单公开投影的内容卫生：每个字符串都会随 /.well-known 匿名公开。

判据源=converge-line-identity-and-keys 的 specs/line-manifest（「清单的每个字符串
都会匿名公开，字符串键落在闭集里」那条）；全量体检=`scripts/line_manifest_check.py`。
2026-11-15 本仓曾有 5 段 note（含环境变量名、内部 ClusterIP、模块/迁移文件路径、
豁免面清单），已逐字移入 docs/line-auth-evidence.md——本测试防它回来。
"""

import json
import re
from pathlib import Path

import pytest

# 本仓 conftest 的 autouse `_reset_db` 会为**没带这个标记**的用例去连真实 Postgres
# 并在缺 DSN 时整条 skip——判据被 skip 掉等于没验（conftest 里那条注释记的就是这件事）。
pytestmark = pytest.mark.no_db

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "line-manifest.json"
EVIDENCE = REPO_ROOT / "docs" / "line-auth-evidence.md"

_SANCTIONED_TEXT_PATHS = {
    "schema", "line", "public_base_url", "internal_base_url",
    "identity.issuer", "identity.jwks_uri", "identity.jit_policy",
    "billing.plane", "billing.evidence_url", "billing.evidence_path",
    "auth_tracks[].name", "auth_tracks[].kind", "auth_tracks[].accepts",
    "auth_tracks[].migration_task", "scopes[]",
}

_INTERNAL_PATTERNS = (
    (r"fd-[a-z0-9][a-z0-9-]*-(?:token|key)\b", "Secret 名"),
    (r"\b[A-Z][A-Z0-9_]{2,}(?:TOKEN|SECRET|PASSWORD|PEM)\b", "凭据类环境变量"),
    (r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+\.(?:py|js|ts|sh|sql|ya?ml)(?::\d+(?:-\d+)?)?", "内部代码路径"),
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b10\.43\.\d+\.\d+\b", "集群内地址"),
    (r"/api/(?:servers|patch-keys|tenants)\b|X-Internal-Token", "内部管理面调用形状"),
    (r"/(?:health|healthz|metrics)\b", "豁免面探针路径"),
)


def _walk_strings(node, path=""):
    if isinstance(node, dict):
        for k in node:
            yield from _walk_strings(node[k], (path + "." + k) if path else k)
    elif isinstance(node, list):
        for i in range(len(node)):
            yield from _walk_strings(node[i], path + "[]")
    elif isinstance(node, str):
        yield path, node


def test_source_manifest_has_no_free_text():
    """清单的每个字符串都会匿名公开 ⇒ 只许产品语义，不许内部细节。

    判据是**键的闭集**而不是长度（20 字符的 note 一样能写出 Secret 名）；
    外加内容扫描（凭据标识 / 内部代码路径 / 集群内地址 / 管理面形状 / 探针路径）。
    `internal_base_url` 是规格允许写集群内地址的唯一字段，排除在内容扫描外。
    """
    doc = json.loads(SOURCE.read_text(encoding="utf-8"))
    strings = list(_walk_strings(doc))

    stray = [p for p, _ in strings if p not in _SANCTIONED_TEXT_PATHS]
    assert not stray, f"清单里出现自由文本键（会随投影公开）: {stray}；证据请写进 docs/"

    body = json.dumps({k: v for k, v in doc.items() if k != "internal_base_url"},
                      ensure_ascii=False)
    for pattern, what in _INTERNAL_PATTERNS:
        hit = re.search(pattern, body)
        assert hit is None, f"清单里出现{what}: {hit.group(0)!r}"


def test_stripped_notes_are_archived_verbatim():
    """剥掉的自由文本必须留在仓内证据文档里——只删不归档等于丢证据。

    五段 note 各有落点：`<root>` / `identity` / `auth_tracks[]`（**两条轨各一段**，
    所以这一节要出现两次）/ `billing`。数两遍是刻意的：只判「这一节在不在」时，
    把其中一条轨的证据删掉仍然全绿。
    """
    assert EVIDENCE.is_file(), (
        f"缺 {EVIDENCE.relative_to(REPO_ROOT)}：清单里的自由文本被删掉了却没有归档，"
        "无法审计当初声明的是什么事实")
    text = EVIDENCE.read_text(encoding="utf-8")
    for pointer in ("### `<root>`", "### `identity`", "### `billing`"):
        assert pointer in text, f"证据文档缺 {pointer} 这一节"
    assert text.count("### `auth_tracks[]`") == 2, "两条 auth_track 的 note 都要归档"
