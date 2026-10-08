"""固定 Git 来源的真实宿主文件系统回归；Windows 仅在 Windows 上运行。"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_user_source_files as source_files
from harnessix.workspace import snapshot

pytestmark = pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="需要实际原生宿主")
_OID = b"a" * 40


@dataclass
class Repository:
    root: Path
    common: Path
    admin: Path

    def pin(self, checkpoint: Callable[[], None] = lambda: None):
        return source_files.pin_git_user_source_files(
            self.root, self.common, self.admin, checkpoint=checkpoint
        )


def _admin_files(directory: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (directory / "config").write_bytes(b"[core]\n\tfilemode = true\n")
    (directory / "packed-refs").write_bytes(_OID + b" refs/heads/packed\n")
    (directory / "refs/heads").mkdir(parents=True)
    (directory / "refs/heads/main").write_bytes(_OID + b"\n")
    (directory / "refs/remotes/origin").mkdir(parents=True)
    (directory / "refs/remotes/origin/main").write_bytes(_OID + b"\n")
    (directory / "reftable").mkdir()
    (directory / "reftable/tables.list").write_bytes(b"0001.ref\n")
    (directory / "reftable/0001.ref").write_bytes(b"fixture reftable table")


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    root = tmp_path.resolve() / "repository"
    root.mkdir()
    common = root / ".git"
    _admin_files(common)
    return Repository(root, common, common)


def _changed(call: Callable[[], None]) -> KernelError:
    with pytest.raises(KernelError) as caught:
        call()
    assert caught.value.code == "git_user_observation_changed"
    return caught.value


def _replace_file(path: Path) -> None:
    body = path.read_bytes()
    info = path.stat()
    replacement = path.parent / (path.name + ".replacement")
    replacement.write_bytes(body)
    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
    os.replace(replacement, path)


def _assert_closed(roots) -> None:
    assert roots
    for native in roots:
        if os.name == "posix":
            assert native._workspace._root_fd is None
        else:
            assert native._root_handle is None


def _track_roots(monkeypatch):
    roots = []
    original = source_files._open_native

    def opened(path, platform):
        native = original(path, platform)
        roots.append(native)
        return native

    monkeypatch.setattr(source_files, "_open_native", opened)
    return roots


def test_unchanged_sources_keep_only_private_summaries(repository):
    with repository.pin() as pinned:
        pinned.verify(lambda: None)
        pinned.verify(lambda: None)
        assert repr(pinned) == "GitUserSourceFiles()"
        assert str(repository.root) not in repr(pinned)
        assert "filemode" not in repr(pinned)
        assert all(not isinstance(node.sha256, bytes) for node in pinned._baseline.values())


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("config", b"[core]\n\tfilemode = false\n"),
        ("HEAD", _OID + b"\n"),
        ("refs/heads/main", b"b" * 40 + b"\n"),
        ("refs/remotes/origin/main", b"b" * 40 + b"\n"),
        ("packed-refs", _OID + b" refs/heads/different\n"),
        ("reftable/0001.ref", b"changed table"),
        ("reftable/tables.list", b"0002.ref\n"),
    ],
)
def test_same_key_or_same_oid_and_source_text_changes_are_detected(repository, path, body):
    with repository.pin() as pinned:
        (repository.common / path).write_bytes(body)
        error = _changed(lambda: pinned.verify(lambda: None))
        assert str(repository.root) not in str(error)
        assert "filemode" not in str(error)
        assert body.decode() not in str(error)


@pytest.mark.parametrize("path", ["config.worktree", "commondir", "HEAD", "packed-refs"])
def test_missing_file_creation_is_detected(repository, path):
    target = repository.common / path
    target.unlink(missing_ok=True)
    with repository.pin() as pinned:
        target.write_bytes(b"new fixed source")
        _changed(lambda: pinned.verify(lambda: None))


@pytest.mark.parametrize("path", ["refs", "reftable"])
def test_missing_tree_creation_is_detected(repository, path):
    target = repository.common / path
    shutil.rmtree(target)
    with repository.pin() as pinned:
        target.mkdir()
        _changed(lambda: pinned.verify(lambda: None))


@pytest.mark.parametrize("path", ["HEAD", "config", "refs/heads/main", "reftable/0001.ref"])
def test_observed_file_deletion_is_detected(repository, path):
    with repository.pin() as pinned:
        (repository.common / path).unlink()
        _changed(lambda: pinned.verify(lambda: None))


@pytest.mark.parametrize("path", ["refs/heads/new", "reftable/0002.ref"])
def test_directory_member_addition_is_detected(repository, path):
    with repository.pin() as pinned:
        (repository.common / path).write_bytes(b"new member")
        _changed(lambda: pinned.verify(lambda: None))


@pytest.mark.parametrize(
    "path", ["config", "HEAD", "packed-refs", "refs/heads/main", "reftable/0001.ref"]
)
def test_same_body_and_mtime_inode_replacement_is_detected(repository, path):
    with repository.pin() as pinned:
        _replace_file(repository.common / path)
        _changed(lambda: pinned.verify(lambda: None))


@pytest.mark.skipif(os.name != "posix", reason="Windows 原生句柄禁止目录重命名")
@pytest.mark.parametrize("path", ["refs", "refs/heads", "reftable"])
def test_same_members_directory_replacement_is_detected(repository, path, tmp_path):
    with repository.pin() as pinned:
        target = repository.common / path
        original = tmp_path / "old-directory"
        target.rename(original)
        shutil.copytree(original, target)
        _changed(lambda: pinned.verify(lambda: None))


@pytest.mark.skipif(os.name != "posix", reason="Windows 原生句柄禁止目录重命名")
@pytest.mark.parametrize("which", ["root", "common", "admin"])
def test_root_common_and_admin_path_replacement_is_detected(repository, tmp_path, which):
    if which == "admin":
        repository.admin = tmp_path.resolve() / "admin"
        _admin_files(repository.admin)
    with repository.pin() as pinned:
        target = getattr(repository, which)
        original = tmp_path / "old-root"
        target.rename(original)
        shutil.copytree(original, target)
        _changed(lambda: pinned.verify(lambda: None))


def test_distinct_admin_and_common_both_contribute_sources(repository, tmp_path, monkeypatch):
    repository.admin = tmp_path.resolve() / "admin"
    _admin_files(repository.admin)
    roots = _track_roots(monkeypatch)
    with repository.pin() as pinned:
        assert len(roots) == 3
        (repository.admin / "config").write_bytes(b"[core]\n\tfilemode = false\n")
        _changed(lambda: pinned.verify(lambda: None))
    _assert_closed(roots)


def test_common_equals_admin_reuses_one_native_handle(repository, monkeypatch):
    roots = _track_roots(monkeypatch)
    with repository.pin() as pinned:
        assert len(roots) == 2
        pinned.verify(lambda: None)
    _assert_closed(roots)
    _changed(lambda: pinned.verify(lambda: None))


def test_file_locator_full_text_and_identity_are_bound(repository, tmp_path):
    common = tmp_path.resolve() / "common"
    repository.common.rename(common)
    repository.common = repository.admin = common
    locator = repository.root / ".git"
    locator.write_bytes(b"gitdir: ../common\n")
    with repository.pin() as pinned:
        locator.write_bytes(b"gitdir: ../common/\n")
        _changed(lambda: pinned.verify(lambda: None))
    with repository.pin() as pinned:
        _replace_file(locator)
        _changed(lambda: pinned.verify(lambda: None))


def test_missing_locator_creation_is_detected(repository, tmp_path):
    common = tmp_path.resolve() / "common"
    repository.common.rename(common)
    repository.common = repository.admin = common
    with repository.pin() as pinned:
        (repository.root / ".git").write_bytes(b"gitdir: ../common\n")
        _changed(lambda: pinned.verify(lambda: None))


def test_unselected_sources_and_locator_directory_members_are_not_captured(repository):
    for directory in ("objects", "logs"):
        (repository.common / directory).mkdir()
        (repository.common / directory / "fixture").write_bytes(b"before")
    for name in ("gitdir", "index"):
        (repository.common / name).write_bytes(b"before")
    with repository.pin() as pinned:
        for directory in ("objects", "logs"):
            (repository.common / directory / "fixture").write_bytes(b"after")
            (repository.common / directory / "new").mkdir()
        for name in ("gitdir", "index"):
            _replace_file(repository.common / name)
            (repository.common / name).write_bytes(b"after")
        (repository.root / "unrelated").write_bytes(b"outside source set")
        pinned.verify(lambda: None)


def test_reftable_dummy_heads_file_is_a_recursive_file(repository):
    heads = repository.common / "refs/heads"
    shutil.rmtree(heads)
    heads.write_bytes(b"this repository uses the reftable format\n")
    with repository.pin() as pinned:
        pinned.verify(lambda: None)
        heads.write_bytes(b"changed dummy heads\n")
        _changed(lambda: pinned.verify(lambda: None))


def _symlink(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        if os.name == "nt":
            pytest.skip("当前 Windows 宿主未授予符号链接创建权限")
        raise


@pytest.mark.parametrize("path", ["config", "refs", "refs/heads/main", "reftable/0001.ref"])
def test_source_symlinks_are_rejected_without_following(repository, tmp_path, path):
    target = repository.common / path
    outside = tmp_path / "outside"
    if target.is_dir():
        target.rename(outside)
    else:
        target.unlink()
        outside.write_bytes(b"not a source")
    _symlink(outside, target)
    with pytest.raises(KernelError) as caught, repository.pin():
        pytest.fail("链接来源不能进入上下文")
    assert caught.value.code == "git_user_observation_path_invalid"
    assert str(outside) not in str(caught.value)


def test_locator_symlink_is_rejected_even_with_separate_physical_common(repository, tmp_path):
    outside = tmp_path.resolve() / "common"
    repository.common.rename(outside)
    repository.common = repository.admin = outside
    _symlink(outside, repository.root / ".git")
    with pytest.raises(KernelError), repository.pin():
        pytest.fail("locator 不能跟随链接")


def test_ancestor_symlink_is_not_normalized_away(repository, tmp_path):
    alias = tmp_path.resolve() / "alias"
    _symlink(repository.root, alias)
    with (
        pytest.raises(KernelError),
        source_files.pin_git_user_source_files(
            alias, repository.common, repository.admin, checkpoint=lambda: None
        ),
    ):
        pytest.fail("根必须为物理路径")


def test_symlink_introduced_after_baseline_is_changed(repository, tmp_path):
    with repository.pin() as pinned:
        target = repository.common / "config"
        target.unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(b"not a source")
        _symlink(outside, target)
        _changed(lambda: pinned.verify(lambda: None))


@pytest.mark.skipif(os.name != "posix", reason="需要 POSIX FIFO")
@pytest.mark.parametrize("path", ["config", "refs/heads/fifo", "reftable/fifo"])
def test_special_objects_are_rejected_without_opening(repository, path):
    target = repository.common / path
    target.unlink(missing_ok=True)
    os.mkfifo(target)
    with pytest.raises(KernelError), repository.pin():
        pytest.fail("特殊对象不能进入上下文")


@pytest.mark.parametrize("path", ["HEAD", "config", "config.worktree", "commondir", "packed-refs"])
def test_fixed_file_slots_reject_directories(repository, path):
    target = repository.common / path
    target.unlink(missing_ok=True)
    target.mkdir()
    with pytest.raises(KernelError) as caught, repository.pin():
        pytest.fail("固定文件来源不能是目录")
    assert caught.value.code == "git_user_observation_path_invalid"


@pytest.mark.parametrize("path", ["refs", "reftable"])
def test_tree_slots_reject_files(repository, path):
    target = repository.common / path
    shutil.rmtree(target)
    target.write_bytes(b"not a tree")
    with pytest.raises(KernelError), repository.pin():
        pytest.fail("递归根不能是文件")


class ParentAbort(BaseException):
    """测试不属于 Exception 的上游取消。"""


@pytest.mark.parametrize(
    "phase", ["entry", "native_capture", "native_entry_verify", "native_verify"]
)
@pytest.mark.parametrize(
    "error",
    [
        KernelError("workspace_snapshot_limit", "上游预创建错误"),
        KernelError("git_user_observation_path_invalid", "上游预创建错误"),
        OSError("上游预创建错误"),
        TimeoutError("上游预创建错误"),
        ParentAbort("上游预创建错误"),
    ],
)
def test_checkpoint_first_exception_instance_survives_real_native_io(
    repository, monkeypatch, phase, error
):
    roots = _track_roots(monkeypatch)
    armed = phase == "entry"
    raised = 0
    config_reads = 0

    def checkpoint():
        nonlocal raised
        if armed:
            raised += 1
            raise error if raised == 1 else OSError("不能覆盖第一次异常")

    if phase == "entry":
        with pytest.raises(type(error)) as caught, repository.pin(checkpoint):
            pytest.fail("控制异常不能进入上下文")
        assert not roots
    else:
        native_type = snapshot._PosixRoot if os.name == "posix" else None
        if native_type is None:
            from harnessix.workspace.windows import WindowsWorkspaceRoot

            native_type = WindowsWorkspaceRoot
        original = native_type.observe

        def observed(self, path, **kwargs):
            nonlocal armed, config_reads
            if path == "config":
                config_reads += 1
                armed = config_reads >= (2 if phase == "native_entry_verify" else 1)
            return original(self, path, **kwargs)

        if phase in {"native_capture", "native_entry_verify"}:
            monkeypatch.setattr(native_type, "observe", observed)
            with pytest.raises(type(error)) as caught, repository.pin(checkpoint):
                pytest.fail("原生捕获必须传播控制异常")
        else:
            with repository.pin() as pinned:
                monkeypatch.setattr(native_type, "observe", observed)
                with pytest.raises(type(error)) as caught:
                    pinned.verify(checkpoint)
        _assert_closed(roots)
    assert caught.value is error
    assert raised == 1


def test_control_latches_first_exception_without_consuming_callback_again():
    original = TimeoutError("第一次控制异常")
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        raise original if calls == 1 else OSError("第二次异常")

    control = source_files._Checkpoint(checkpoint)
    for _ in range(2):
        with pytest.raises(source_files.UpstreamCheckpointError) as caught:
            control()
        assert caught.value.error is original
    assert calls == 1


@pytest.mark.parametrize("body_error", [None, OSError("正文异常"), ParentAbort("正文取消")])
def test_all_owned_handles_close_on_normal_and_exceptional_exit(
    repository, monkeypatch, body_error
):
    roots = _track_roots(monkeypatch)
    if body_error is None:
        with repository.pin():
            pass
    else:
        with pytest.raises(type(body_error)) as caught, repository.pin():
            raise body_error
        assert caught.value is body_error
    _assert_closed(roots)


def test_partial_root_open_failure_closes_already_opened_handles(repository, monkeypatch):
    roots = _track_roots(monkeypatch)
    missing = repository.root.parent / "missing-admin"
    with (
        pytest.raises(KernelError),
        source_files.pin_git_user_source_files(
            repository.root, repository.common, missing, checkpoint=lambda: None
        ),
    ):
        pytest.fail("缺失原生根不能进入上下文")
    assert len(roots) == 2
    _assert_closed(roots)


def test_cleanup_failures_never_mask_body_exception_and_all_roots_are_closed(
    repository, monkeypatch
):
    roots = _track_roots(monkeypatch)
    body_error = TimeoutError("正文控制异常")
    with pytest.raises(TimeoutError) as caught, repository.pin():
        for native in roots:
            original = native.close

            def close(original=original):
                original()
                raise OSError("关闭异常中的私有路径")

            monkeypatch.setattr(native, "close", close)
        raise body_error
    assert caught.value is body_error
    _assert_closed(roots)


def test_exit_does_not_verify_or_consume_another_checkpoint(repository):
    exited = False

    def checkpoint():
        if exited:
            pytest.fail("finally 不能复核或调用 checkpoint")

    with repository.pin(checkpoint):
        (repository.common / "config").write_bytes(b"changed without explicit verify")
        exited = True


@pytest.mark.parametrize("limit_name", ["MAX_SOURCE_NODES", "MAX_SOURCE_BYTES"])
def test_aggregate_limits_are_exact_and_fail_closed_with_small_thresholds(
    repository, monkeypatch, limit_name
):
    with repository.pin() as pinned:
        threshold = (
            len(pinned._baseline) + len(pinned._roots)
            if limit_name == "MAX_SOURCE_NODES"
            else sum(node.size for node in pinned._baseline.values() if node.kind == "file")
        )
    monkeypatch.setattr(source_files, limit_name, threshold)
    with repository.pin() as pinned:
        pinned.verify(lambda: None)
    monkeypatch.setattr(source_files, limit_name, threshold - 1)
    with pytest.raises(KernelError) as caught, repository.pin():
        pytest.fail("超额来源不能进入上下文")
    assert caught.value.code == "git_user_source_limit"


def test_verify_over_limit_has_fixed_source_limit_error(repository, monkeypatch):
    with repository.pin() as pinned:
        monkeypatch.setattr(source_files, "MAX_SOURCE_BYTES", 1)
        with pytest.raises(KernelError) as caught:
            pinned.verify(lambda: None)
    assert caught.value.code == "git_user_source_limit"


def test_native_single_file_limit_is_mapped_and_closes_roots(repository, monkeypatch):
    roots = _track_roots(monkeypatch)
    with (repository.common / "config").open("wb") as stream:
        stream.truncate(snapshot.MAX_SNAPSHOT_FILE_BYTES + 1)
    with pytest.raises(KernelError) as caught, repository.pin():
        pytest.fail("单文件不得超过原生 8 MiB")
    assert caught.value.code == "git_user_source_limit"
    _assert_closed(roots)


def test_native_directory_entry_limit_uses_fixed_source_limit_error(repository, monkeypatch):
    port = snapshot
    if os.name == "nt":
        from harnessix.workspace import windows

        port = windows
    monkeypatch.setattr(port, "MAX_SNAPSHOT_DIRECTORY_ENTRIES", 1)
    with pytest.raises(KernelError) as caught, repository.pin():
        pytest.fail("原生目录枚举过限必须拒绝")
    assert caught.value.code == "git_user_source_limit"


@pytest.mark.parametrize("mutation", ["content", "inode", "membership", "parent_missing"])
def test_mutation_during_capture_is_rejected(repository, monkeypatch, mutation):
    native_type = snapshot._PosixRoot if os.name == "posix" else None
    if native_type is None:
        from harnessix.workspace.windows import WindowsWorkspaceRoot

        native_type = WindowsWorkspaceRoot
    original = native_type.observe
    changed = False

    def observed(self, path, **kwargs):
        nonlocal changed
        result = original(self, path, **kwargs)
        target = "refs/heads" if mutation in {"membership", "parent_missing"} else "config"
        if not changed and path == target:
            changed = True
            if mutation == "content":
                (repository.common / "config").write_bytes(b"[core]\n\tfilemode = false\n")
            elif mutation == "inode":
                _replace_file(repository.common / "config")
            elif mutation == "membership":
                (repository.common / "refs/heads/added").write_bytes(_OID)
            else:
                shutil.rmtree(repository.common / "refs/heads")
        return result

    monkeypatch.setattr(native_type, "observe", observed)
    with pytest.raises(KernelError) as caught, repository.pin():
        pytest.fail("捕获过程中变更不能成为基线")
    assert changed
    assert caught.value.code == "git_user_observation_changed"


def test_directory_changes_between_member_observations_are_rejected(repository, monkeypatch):
    native_type = snapshot._PosixRoot if os.name == "posix" else None
    if native_type is None:
        from harnessix.workspace.windows import WindowsWorkspaceRoot

        native_type = WindowsWorkspaceRoot
    original = native_type.observe
    armed = False

    def observed(self, path, **kwargs):
        nonlocal armed
        result = original(self, path, **kwargs)
        if armed and path == "refs/heads/main":
            (repository.common / "refs/heads/late").write_bytes(_OID)
            armed = False
        return result

    with repository.pin() as pinned:
        armed = True
        monkeypatch.setattr(native_type, "observe", observed)
        _changed(lambda: pinned.verify(lambda: None))


def test_symlink_replacement_during_directory_capture_is_changed(repository, monkeypatch, tmp_path):
    native_type = snapshot._PosixRoot if os.name == "posix" else None
    if native_type is None:
        from harnessix.workspace.windows import WindowsWorkspaceRoot

        native_type = WindowsWorkspaceRoot
    original = native_type.observe
    replaced = False

    def observed(self, path, **kwargs):
        nonlocal replaced
        result = original(self, path, **kwargs)
        if path == "refs/heads" and not replaced:
            replaced = True
            target = repository.common / path
            outside = tmp_path / "outside-heads"
            target.rename(outside)
            _symlink(outside, target)
        return result

    monkeypatch.setattr(native_type, "observe", observed)
    with pytest.raises(KernelError) as caught, repository.pin():
        pytest.fail("已观察目录不能在捕获中替换成链接")
    assert replaced
    assert caught.value.code == "git_user_observation_changed"


@pytest.mark.skipif(os.name != "posix", reason="需要 POSIX 路径名语义")
@pytest.mark.parametrize("name", ["bad\\name", "bad\nname"])
def test_recursive_members_retain_native_path_validation(repository, name):
    (repository.common / "refs/heads" / name).write_bytes(_OID)
    with pytest.raises(KernelError), repository.pin():
        pytest.fail("递归成员必须满足原端口路径规范")


@pytest.mark.skipif(os.name != "posix", reason="需要 POSIX 深层目录")
def test_iterative_walk_keeps_original_native_depth_limit(repository):
    deep = repository.common / "refs"
    for _ in range(128):
        deep /= "d"
        deep.mkdir()
    with pytest.raises(KernelError), repository.pin():
        pytest.fail("迭代遍历不能放宽原生 128 段上限")


def test_no_subprocess_or_writable_posix_open_is_used(repository, monkeypatch):
    def process_denied(*args, **kwargs):
        pytest.fail("原生来源 primitive 不得启动子进程")

    monkeypatch.setattr(subprocess, "Popen", process_denied)
    if os.name == "posix":
        original = os.open

        def opened(path, flags, *args, **kwargs):
            assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            return original(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", opened)
    with repository.pin() as pinned:
        pinned.verify(lambda: None)


@pytest.mark.skipif(os.name != "nt", reason="只在实际 Windows 宿主验证原生端口")
def test_actual_windows_root_handle_port(repository):
    from harnessix.workspace.windows import WindowsWorkspaceRoot

    with repository.pin() as pinned:
        assert all(isinstance(native, WindowsWorkspaceRoot) for native in pinned._roots)
        pinned.verify(lambda: None)
