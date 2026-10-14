"""封存发布后暂存别名的认证恢复；未知文件和原始审计证据不可清理。"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

import harnessix.evals.swebench.workspace_retirement as retirement
import harnessix.evals.swebench.workspace_retirement_fs as retirement_fs
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from tests.evals.swebench.test_workspace_retirement import KEY, Case, _case, _file


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return _case(tmp_path)


def _temporary(target: Path) -> Path:
    return target.with_name(f".{target.name}.tmp")


def _snapshot(path: Path) -> tuple[object, ...]:
    info = path.lstat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        path.read_bytes(),
    )


def _pause_retirement(case: Case, stage: str) -> None:
    def pause(point: str) -> None:
        if point == stage:
            raise RuntimeError("在已发布边界暂停，保留原工作区")

    with pytest.raises(RuntimeError):
        case.retire(fault=pause)


def _interrupt_publication(case: Case, monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    original_unlink = os.unlink

    def fail_alias_unlink(name, *args, **kwargs):
        if name == _temporary(target).name and kwargs.get("dir_fd") is not None:
            raise OSError(errno.EIO, "模拟发布成功后暂存别名删除失败")
        return original_unlink(name, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(retirement_fs.os, "unlink", fail_alias_unlink)
        with pytest.raises(KernelError, match="SWE-bench"):
            case.retire()
    assert target.samefile(_temporary(target))
    assert target.stat().st_nlink == 2
    assert (case.workspace / "example.py").exists()
    case.assert_preserved()


def _assert_read_only(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = list(case.preserved)
    for target in (case.seal, case.marker):
        paths.extend(path for path in (target, _temporary(target)) if path.exists())
    before = {path: _snapshot(path) for path in paths}

    def forbidden_unlink(*_args, **_kwargs):
        pytest.fail("读取封存不得回收任何暂存别名")

    with monkeypatch.context() as reading:
        reading.setattr(retirement_fs.os, "unlink", forbidden_unlink)
        assert case.read() == case.patch
    assert {path: _snapshot(path) for path in paths} == before


@pytest.mark.parametrize("target_name", ["seal", "marker"])
def test_published_alias_read_is_pure_and_retirement_recovers(
    case: Case, monkeypatch: pytest.MonkeyPatch, target_name: str
) -> None:
    target = getattr(case, target_name)
    _interrupt_publication(case, monkeypatch, target)
    before = target.stat().st_ino, target.read_bytes()
    _assert_read_only(case, monkeypatch)
    manifest = case.retire()
    assert not _temporary(target).exists()
    assert target.stat().st_nlink == 1
    assert (target.stat().st_ino, target.read_bytes()) == before
    assert manifest.patch == case.patch
    assert list(case.workspace.iterdir()) == [case.marker]
    case.assert_preserved()
    _assert_read_only(case, monkeypatch)
    assert case.retire() == manifest


def test_both_published_aliases_recover_without_republishing(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    _interrupt_publication(case, monkeypatch, case.seal)
    _interrupt_publication(case, monkeypatch, case.marker)
    _assert_read_only(case, monkeypatch)
    images = {
        target: (target.stat().st_ino, target.read_bytes()) for target in (case.seal, case.marker)
    }
    case.retire()
    for target, image in images.items():
        assert (target.stat().st_ino, target.read_bytes()) == image
        assert target.stat().st_nlink == 1 and not _temporary(target).exists()
    case.assert_preserved()


@pytest.mark.parametrize("target_name", ["seal", "marker"])
@pytest.mark.parametrize("pending_alias", [False, True])
def test_third_party_hardlinks_are_rejected_without_deletion(
    case: Case,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    pending_alias: bool,
) -> None:
    target = getattr(case, target_name)
    if pending_alias:
        _interrupt_publication(case, monkeypatch, target)
    else:
        _pause_retirement(case, "after_marker")
    external = tmp_path / "第三方硬链接"
    os.link(target, external)
    before = _snapshot(target), _snapshot(external)
    for operation in (case.read, case.retire):
        with pytest.raises(KernelError):
            operation()
    assert (_snapshot(target), _snapshot(external)) == before
    assert _temporary(target).exists() == pending_alias
    assert (case.workspace / "example.py").exists()
    case.assert_preserved()


@pytest.mark.parametrize("target_name", ["seal", "marker"])
@pytest.mark.parametrize("kind", ["different_inode", "symlink", "directory"])
def test_unknown_temporary_is_neither_overwritten_nor_removed(
    case: Case, target_name: str, kind: str
) -> None:
    _pause_retirement(case, "after_marker")
    target = getattr(case, target_name)
    temporary = _temporary(target)
    if kind == "different_inode":
        _file(temporary, target.read_bytes())
    elif kind == "symlink":
        temporary.symlink_to(target)
    else:
        temporary.mkdir(mode=0o700)
    before = temporary.lstat(), _snapshot(target)
    for operation in (case.read, case.retire):
        with pytest.raises(KernelError):
            operation()
    assert (temporary.lstat(), _snapshot(target)) == before
    assert (case.workspace / "example.py").exists()
    case.assert_preserved()


@pytest.mark.parametrize("target_name", ["seal", "marker"])
def test_unpublished_temporary_is_rejected_without_unsafe_recovery(
    case: Case, target_name: str
) -> None:
    target = getattr(case, target_name)
    if target_name == "marker":
        _pause_retirement(case, "after_seal")
    temporary = _temporary(target)
    _file(temporary, "未发布或未写完的未知材料".encode())
    before = _snapshot(temporary)
    for operation in (case.read, case.retire):
        with pytest.raises(KernelError):
            operation()
    assert not target.exists() and _snapshot(temporary) == before
    assert (case.workspace / "example.py").exists()
    case.assert_preserved()


@pytest.mark.parametrize("target_name", ["seal", "marker"])
@pytest.mark.parametrize("invalid", ["key", "native", "state", "seal", "marker"])
def test_authentication_failure_never_recovers_a_published_alias(
    case: Case, monkeypatch: pytest.MonkeyPatch, target_name: str, invalid: str
) -> None:
    if invalid == "marker" and target_name == "seal":
        _pause_retirement(case, "after_marker")
        os.link(case.seal, _temporary(case.seal))
    else:
        _interrupt_publication(case, monkeypatch, getattr(case, target_name))
    if invalid != "key":
        path = case.state_file if invalid == "state" else getattr(case, invalid)
        with path.open("ab") as output:
            output.write("证据漂移".encode())
    target = getattr(case, target_name)
    temporary = _temporary(target)
    before = _snapshot(target), _snapshot(temporary)
    key = "错误的认证密钥" if invalid == "key" else KEY
    with pytest.raises(KernelError):
        case.read(key=key)
    with pytest.raises(KernelError):
        retirement.seal_and_retire_workspace(
            case.plan, case.config, 0, case.state, case.patch, api_key=key
        )
    assert (_snapshot(target), _snapshot(temporary)) == before
    assert (case.workspace / "example.py").exists()


@pytest.mark.parametrize("target_name", ["native", "state_file"])
def test_native_and_state_remain_strictly_single_link(
    case: Case, monkeypatch: pytest.MonkeyPatch, target_name: str
) -> None:
    _interrupt_publication(case, monkeypatch, case.seal)
    target = getattr(case, target_name)
    temporary = _temporary(target)
    os.link(target, temporary)
    before = _snapshot(target), _snapshot(temporary), _snapshot(_temporary(case.seal))
    for operation in (case.read, case.retire):
        with pytest.raises(KernelError):
            operation()
    assert (_snapshot(target), _snapshot(temporary), _snapshot(_temporary(case.seal))) == before
    assert (case.workspace / "example.py").exists()


@pytest.mark.parametrize("target_name", ["seal", "marker"])
def test_cancelled_retirement_does_not_recover_published_alias(
    case: Case, monkeypatch: pytest.MonkeyPatch, target_name: str
) -> None:
    target = getattr(case, target_name)
    _interrupt_publication(case, monkeypatch, target)
    before = _snapshot(target), _snapshot(_temporary(target))
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(TurnCancelled):
        case.retire(cancel=cancel)
    assert (_snapshot(target), _snapshot(_temporary(target))) == before
    case.assert_preserved()


def test_contract_and_cli_import_without_posix_open_flags() -> None:
    """模拟缺少 Windows 上不存在的常量，只验证导入，不宣称可执行退役。"""
    script = """
import os
for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
    if hasattr(os, name):
        delattr(os, name)
import harnessix.evals.swebench.run_config
import harnessix.evals.swebench.cli
import harnessix.evals.swebench.workspace_retirement_fs
print("平台无关合同与CLI导入成功")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "平台无关合同与CLI导入成功" in result.stdout


@pytest.mark.parametrize("missing", ["O_DIRECTORY", "O_NOFOLLOW", "non_posix"])
def test_missing_retirement_capability_fails_before_open_or_mutation(
    case: Case, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """不支持的执行平台在描述符入口拒绝，不创建、删除或发布任何文件。"""
    scope = retirement._scope(case.plan, case.config, 0)

    def forbidden_operation(*_args, **_kwargs):
        pytest.fail("缺少退役能力时不得打开或修改文件系统")

    with monkeypatch.context() as unsupported:
        if missing == "non_posix":
            unsupported.setattr(retirement_fs.os, "name", "nt")
        else:
            unsupported.delattr(retirement_fs.os, missing)
        for operation in ("open", "mkdir", "link", "unlink"):
            unsupported.setattr(retirement_fs.os, operation, forbidden_operation)
        with pytest.raises(KernelError):
            with retirement_fs._opened(scope):
                pytest.fail("不支持的平台不得进入退役作用域")
        if missing != "non_posix":
            for operation in (case.read, case.retire):
                with pytest.raises(KernelError):
                    operation()
    assert not case.seal.exists() and not case.marker.exists()
    assert (case.workspace / "example.py").exists()
    case.assert_preserved()
