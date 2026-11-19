"""ADR 0172：补丁可以写密钥与证书文件，凭据名称在两个平台上都受保护，内容从不显示。

- 写目标策略只放开.key、.pem、.p12、.pfx后缀；读取工具的默认策略与默认Workspace不变。
- .git、.harnessix、.codex、.ssh、.aws、.gnupg、.env*与id_rsa等在POSIX与Windows规划中都拒绝；
  Windows根不按文件名过滤，只靠_protected。
- Workspace事务的Diff对这类文件只写前后摘要；Git树Diff不变。
- 编辑锚点失败对这类文件只给通用说明。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceFileVersion, WorkspaceMutation
from harnessix.delivery.diff_content import build_diff_content
from harnessix.delivery.planner import (
    DesiredWorkspaceFile,
    _protected,
    prepare_workspace_transaction,
)
from harnessix.delivery.trusted_action import _normalized_files
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from harnessix.patches.contracts import ExactEdit
from harnessix.patches.planner import apply_exact_edits
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import (
    PatchTargetPolicy,
    ReadOperation,
    Workspace,
    WorkspaceReadPolicy,
    is_key_or_certificate_file,
)

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX规划在当前宿主上才能观察工作区")
PROTECTED = [
    ".git/config",
    ".harnessix/state",
    "pkg/.codex/x",
    ".ssh/config",
    "home/.aws/credentials",
    ".gnupg/pubring.kbx",
    ".env",
    ".ENV.local",
    "deploy/.env.production",
    "id_rsa",
    "keys/ID_DSA",
    "id_ecdsa",
    "a/id_ed25519",
]
KEY_FILES = ["server.key", "certs/a.PEM", "bundle.p12", "x/y.pfx", "certs.pem/readme.txt"]


def _proposal(path: str) -> WorkspacePatchInput:
    return WorkspacePatchInput.model_validate(
        {"files": [{"operation": "create", "path": path, "content": "x\n", "mode": 420}]}
    )


@pytest.mark.parametrize("platform", ["posix", "windows"])
@pytest.mark.parametrize("path", PROTECTED)
def test_credential_names_are_protected_on_both_platforms(path: str, platform) -> None:
    assert _protected(path, platform)
    with pytest.raises(KernelError) as denied:
        _normalized_files(_proposal(path), platform)
    assert denied.value.code == "delivery_path_denied"


@pytest.mark.parametrize("platform", ["posix", "windows"])
@pytest.mark.parametrize("path", KEY_FILES)
def test_key_and_certificate_files_are_not_protected(path: str, platform) -> None:
    assert not _protected(path, platform)
    assert [item for item, _ in _normalized_files(_proposal(path), platform)] == [path]


@pytest.mark.parametrize("platform", ["posix", "windows"])
@pytest.mark.parametrize("path", [".ssh/config", ".env", "id_rsa"])
def test_transaction_planner_rejects_credential_names(tmp_path: Path, platform, path) -> None:
    """Windows用platform="windows"在任何宿主验证：拒绝发生在观察工作区之前。"""
    if platform == "posix" and os.name != "posix":
        pytest.skip("POSIX规划只在POSIX宿主运行")
    with pytest.raises(KernelError) as denied:
        prepare_workspace_transaction(
            tmp_path,
            {path: DesiredWorkspaceFile(b"secret\n", 0o644)},
            request_id="credential",
            platform=platform,
        )
    assert denied.value.code == "delivery_path_denied"
    assert list(tmp_path.iterdir()) == []


@POSIX_ONLY
def test_posix_transaction_planner_plans_key_files_below_a_certificate_named_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "certs.pem").mkdir()
    (tmp_path / "certs.pem/old.key").write_bytes(b"old\n")
    prepared = prepare_workspace_transaction(
        tmp_path,
        {
            "certs.pem/old.key": DesiredWorkspaceFile(b"new\n", 0o644),
            "certs.pem/new.p12": DesiredWorkspaceFile(b"p12\n", 0o644),
        },
        request_id="keys",
        platform="posix",
    )
    assert [item.path for item in prepared.plan.mutations] == [
        "certs.pem/new.p12",
        "certs.pem/old.key",
    ]
    assert prepared.blobs[hashlib.sha256(b"old\n").hexdigest()] == b"old\n"


@pytest.mark.parametrize("path", ["a.pem", "certs/server.KEY", "certs.p12/x.txt", "b.pfx"])
def test_default_read_policy_still_denies_key_and_certificate_paths(path: str) -> None:
    with pytest.raises(ReadToolError) as denied:
        WorkspaceReadPolicy().parts(path)
    assert denied.value.code == "path_denied"
    assert PatchTargetPolicy().parts(path) == tuple(path.split("/"))


@pytest.mark.parametrize("path", [".ssh/config", ".env.local", "id_rsa", ".git/HEAD", ".aws/x"])
def test_patch_target_policy_keeps_sensitive_names(path: str) -> None:
    with pytest.raises(ReadToolError):
        PatchTargetPolicy().parts(path)


@POSIX_ONLY
def test_default_workspace_keeps_the_read_policy(tmp_path: Path) -> None:
    (tmp_path / "a.pem").write_bytes(b"x")
    with Workspace(tmp_path) as workspace:
        default_scope = workspace.scope
        assert type(workspace._policy) is WorkspaceReadPolicy
        assert workspace._policy.scope_fields()["denied_suffixes"] == (
            ".pem",
            ".key",
            ".p12",
            ".pfx",
        )
        with (
            pytest.raises(ReadToolError),
            workspace.open("a.pem", ReadOperation(), directory=False),
        ):
            pass
    with Workspace(tmp_path, patch_targets=True) as patch:
        assert patch.scope != default_scope
        with patch.open("a.pem", ReadOperation(), directory=False) as descriptor:
            assert os.read(descriptor, 1) == b"x"


@pytest.mark.parametrize(
    ("path", "hidden"),
    [("a.key", True), ("d/B.PEM", True), ("x.p12", True), ("x.pfx", True), ("certs.pem/a", False)],
)
def test_key_or_certificate_file_is_decided_by_the_final_name(path: str, hidden: bool) -> None:
    assert is_key_or_certificate_file(path) is hidden


def _mutation(path: str, before: bytes | None, after: bytes | None) -> WorkspaceMutation:
    def version(body: bytes | None) -> WorkspaceFileVersion:
        if body is None:
            return WorkspaceFileVersion(presence="absent", size=0)
        return WorkspaceFileVersion(
            presence="file", sha256=hashlib.sha256(body).hexdigest(), size=len(body), mode=420
        )

    return WorkspaceMutation(path=path, before=version(before), after=version(after))


def test_workspace_diff_hides_key_content_and_git_diff_is_unchanged() -> None:
    before, after = b"SECRET-BEFORE\n", b"SECRET-AFTER\n"
    mutations = (_mutation("certs/server.key", before, after), _mutation("ok.txt", None, b"ok\n"))
    blobs = {hashlib.sha256(body).hexdigest(): body for body in (before, after, b"ok\n")}

    def build(hide: bool):
        return build_diff_content(
            mutations,
            blobs.__getitem__,
            max_utf8_bytes=None,
            checkpoint=lambda: None,
            hide_key_files=hide,
        )

    hidden = build(True)
    assert "SECRET" not in hidden.text and "+ok" in hidden.text
    assert (
        "diff --harnessix a/certs/server.key b/certs/server.key\n"
        "Content hidden (key or certificate file): "
        f"before={hashlib.sha256(before).hexdigest()} after={hashlib.sha256(after).hexdigest()}\n"
    ) in hidden.text
    # 条目元数据（类别、版本摘要）照常；Git树Diff（不传hide_key_files）字节不变。
    assert [entry.kind for entry in hidden.entries] == ["modified", "added"]
    shown = build(False)
    assert "-SECRET-BEFORE" in shown.text and "+SECRET-AFTER" in shown.text


@pytest.mark.parametrize(
    ("edits", "code", "detail"),
    [
        ((ExactEdit(old_text="MISSING", new_text="x"),), "patch_context_not_found", "line 1"),
        ((ExactEdit(old_text="dup", new_text="x"),), "patch_ambiguous_context", "lines 1, 2"),
        (
            (
                ExactEdit(old_text="dup\ndup", new_text="x"),
                ExactEdit(old_text="dup\nend", new_text="y"),
            ),
            "patch_overlapping_edits",
            "line 2",
        ),
    ],
)
def test_concealed_anchor_failures_have_no_location(edits, code, detail) -> None:
    before = b"dup\ndup\nend\n"
    if code == "patch_context_not_found":
        # 非隐藏时会报告新文本已在第1行，或首个非空行所在的行号。
        edits = (ExactEdit(old_text="dup\nMISSING", new_text="x"),)
    with pytest.raises(KernelError) as plain:
        apply_exact_edits(before, edits, ReadOperation())
    assert plain.value.code == code and detail in plain.value.message
    with pytest.raises(KernelError) as concealed:
        apply_exact_edits(before, edits, ReadOperation(), conceal_content=True)
    assert concealed.value.code == code
    message = concealed.value.message
    assert message.startswith("edits[")
    assert "line" not in message and "dup" not in message and "MISSING" not in message
