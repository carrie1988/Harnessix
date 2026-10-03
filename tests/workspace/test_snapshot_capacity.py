"""以真实资源验证 cwd 补齐后的 Snapshot 容量准入与既有拒绝语义。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot


def _files(root: Path, count: int) -> tuple[WorkspaceResourceRequest, ...]:
    """创建微小普通文件；不替换原生观察器或 Snapshot 返回值。"""
    paths = tuple(f"f{index:03}" for index in range(count))
    for path in paths:
        (root / path).write_bytes(b"before\n")
    return tuple(WorkspaceResourceRequest(path=path, access="read") for path in paths)


@pytest.mark.parametrize("cwd", [".", "src"])
def test_full_capacity_explicit_and_implicit_cwd_agree(tmp_path: Path, cwd: str) -> None:
    """255 个叶及 cwd 正好 256 项；显隐请求生成相同原事实，均可重新验真。"""
    if cwd != ".":
        (tmp_path / cwd).mkdir()
    requests = _files(tmp_path, 255)
    implicit = capture_workspace_snapshot(tmp_path, cwd=cwd, resources=requests)
    explicit = capture_workspace_snapshot(
        tmp_path,
        cwd=cwd,
        resources=(*requests, WorkspaceResourceRequest(path=cwd, access="read")),
    )
    assert implicit == explicit
    assert len(implicit.resources) == 256
    assert {(item.path, item.access) for item in implicit.resources} == {
        *((request.path, "read") for request in requests),
        (cwd, "read"),
    }
    assert verify_workspace_snapshot(implicit, tmp_path) == implicit


@pytest.mark.parametrize("explicit", [False, True])
def test_overflow_counts_implicit_cwd(tmp_path: Path, explicit: bool) -> None:
    """256 个叶加 cwd 一律返回固定限额错误，不泄漏合同构造异常。"""
    requests = _files(tmp_path, 256)
    if explicit:
        requests = (*requests, WorkspaceResourceRequest(path=".", access="read"))
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(tmp_path, resources=requests)
    assert error.value.code == "workspace_snapshot_limit"
    assert all(
        (tmp_path / request.path).read_bytes() == b"before\n"
        for request in requests
        if request.path != "."
    )


@pytest.mark.parametrize("access", ["write", "execute"])
def test_same_path_other_access_still_requires_read_cwd(tmp_path: Path, access: str) -> None:
    """cwd 的其他访问模式不抵扣必需 read 观察，不能合并访问权限绕过上限。"""
    requests = (*_files(tmp_path, 255), WorkspaceResourceRequest(path=".", access=access))
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(tmp_path, resources=requests)
    assert error.value.code == "workspace_snapshot_limit"


def test_external_read_cwd_does_not_replace_workspace_cwd(tmp_path: Path) -> None:
    """不同 location 的同名 read 不抵扣 Workspace cwd，外部访问授权仍显式。"""
    root, external = tmp_path / "repo", tmp_path / "external"
    root.mkdir()
    external.mkdir()
    requests = (
        *_files(root, 255),
        WorkspaceResourceRequest(location="cache", path=".", access="read"),
    )
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(
            root, resources=requests, external_roots={"cache": (external, ("read",))}
        )
    assert error.value.code == "workspace_snapshot_limit"


def test_other_workspace_directory_does_not_replace_cwd(tmp_path: Path) -> None:
    """另一个目录的 read 不能替代当前 cwd，根身份与选择目录仍分别绑定。"""
    (tmp_path / "src").mkdir()
    requests = (*_files(tmp_path, 255), WorkspaceResourceRequest(path=".", access="read"))
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(tmp_path, cwd="src", resources=requests)
    assert error.value.code == "workspace_snapshot_limit"


@pytest.mark.skipif(os.name != "posix", reason="实际 POSIX 观察顺序")
def test_cwd_admission_precedes_file_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只旁观原观察调用；超限时已验 cwd，但尚未逐叶读取正文。"""
    from harnessix.workspace import snapshot as module

    requests = _files(tmp_path, 256)
    observed: list[str] = []
    original = module._PosixRoot.observe

    def observe(root: module._PosixRoot, path: str, *, access: str):
        observed.append(path)
        return original(root, path, access=access)

    monkeypatch.setattr(module._PosixRoot, "observe", observe)
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(tmp_path, resources=requests)
    assert error.value.code == "workspace_snapshot_limit"
    assert observed == ["."]


def test_duplicate_inside_admitted_capacity_remains_rejected(tmp_path: Path) -> None:
    """256 项含 cwd 的输入仍逐项拒绝重复资源，不将请求集合去重后放行。"""
    requests = _files(tmp_path, 254)
    requests = (*requests, requests[0], WorkspaceResourceRequest(path=".", access="read"))
    assert len(requests) == 256
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(tmp_path, resources=requests)
    assert error.value.code == "workspace_snapshot_duplicate"
