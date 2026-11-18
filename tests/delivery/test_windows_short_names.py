"""ADR 0170：Windows 8.3短名称（ENV~1、GIT~1）会被系统解析到长名称，绕过按名称的保护。

2026-11-17在Windows 11真机（D:盘默认开启8.3名称）上用0.10.0b48复现：read_file("ENV~1")把.env内容
交给模型；Patch新建GIT~1/x.txt写进了.git。模型提交的读写路径在Windows上拒绝“~数字”形状的段，
POSIX上这只是普通文件名。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.trusted_action import _normalized_files
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from harnessix.workspace.paths import has_windows_short_name


@pytest.mark.parametrize(
    ("path", "short"),
    [
        ("ENV~1", True),
        ("GIT~1/hooks/pre-commit", True),
        ("src/PROGRA~12.TXT", True),
        ("a/b/HARNES~1/x", True),
        ("notes~", False),
        ("a~b.txt", False),
        ("~/x", False),
        (".env", False),
    ],
)
def test_short_name_shape(path: str, short: bool) -> None:
    assert has_windows_short_name(path) is short


def _proposal(path: str) -> WorkspacePatchInput:
    return WorkspacePatchInput.model_validate(
        {"files": [{"operation": "create", "path": path, "content": "x\n", "mode": 420}]}
    )


@pytest.mark.parametrize("path", ["GIT~1/hx-probe.txt", "src/ENV~1", "HARNES~1/state"])
def test_windows_patch_rejects_short_names_as_a_protected_path(path: str) -> None:
    with pytest.raises(KernelError) as denied:
        _normalized_files(_proposal(path), "windows")
    assert denied.value.code == "delivery_path_denied"


def test_posix_patch_keeps_tilde_digit_names() -> None:
    assert [path for path, _ in _normalized_files(_proposal("docs/a~1.txt"), "posix")] == [
        "docs/a~1.txt"
    ]


def test_windows_transaction_planner_rejects_short_names(tmp_path: Path) -> None:
    with pytest.raises(KernelError) as denied:
        prepare_workspace_transaction(
            tmp_path,
            {"GIT~1/config": DesiredWorkspaceFile(b"x", 0o644)},
            request_id="short-name",
            platform="windows",
        )
    assert denied.value.code == "delivery_path_denied"
