from __future__ import annotations

import os

import pytest

from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import ReadOperation, Workspace


@pytest.mark.parametrize("change", ["root", "directory", "file", "content"])
def test_change_during_read_discards_result_and_closes_descriptors(tmp_path, change):
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    file = root / "src/a.py"
    file.write_text("旧内容")
    with Workspace(root) as workspace:
        with pytest.raises(ReadToolError, match="workspace_changed"):
            with workspace.open("src/a.py", ReadOperation(), directory=False) as fd:
                assert os.read(fd, 100).decode() == "旧内容"
                if change == "root":
                    root.rename(tmp_path / "old")
                    root.mkdir()
                elif change == "directory":
                    (root / "src").rename(root / "old")
                    (root / "src").mkdir()
                elif change == "file":
                    file.rename(root / "src/old.py")
                    file.write_text("新对象")
                else:
                    file.write_text("修改同一 inode 内容")
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("replacement", ["symlink", "file", "fifo"])
def test_replace_between_stat_and_open_never_reads_new_object(tmp_path, monkeypatch, replacement):
    file = tmp_path / "target"
    file.write_text("原对象")
    (tmp_path / "outside").write_text("禁止读取")
    real_open = os.open
    with Workspace(tmp_path) as workspace:

        def swap(path, flags, *args, **kwargs):
            if path == "target":
                file.rename(tmp_path / "old")
                if replacement == "symlink":
                    file.symlink_to(tmp_path / "outside")
                elif replacement == "fifo":
                    os.mkfifo(file)
                else:
                    file.write_text("不同对象")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", swap)
        with pytest.raises(ReadToolError):
            with workspace.open("target", ReadOperation(), directory=False):
                pytest.fail("替换后的对象不得进入读取代码")


def test_root_replacement_between_calls_fails_and_same_root_reopens(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with Workspace(root) as first, Workspace(root) as second:
        assert first.scope == second.scope
        root.rename(tmp_path / "old")
        root.mkdir()
        with Workspace(root) as changed:
            assert first.scope != changed.scope
        with pytest.raises(ReadToolError, match="workspace_changed"):
            with first.open(".", ReadOperation(), directory=True):
                pytest.fail("不得访问替换后的根")


@pytest.mark.parametrize("path", ["", "a/", "a/" * 65 + "b", "中" * 400, "\udcff", "\x1b"])
def test_invalid_path_before_os_access(tmp_path, path):
    with Workspace(tmp_path) as workspace:
        with pytest.raises(ReadToolError, match="path_denied"):
            workspace.parts(path)


def test_policy_is_case_insensitive_component_prefix_not_string_prefix(tmp_path):
    with Workspace(tmp_path, denied_paths=("Private/keys",)) as workspace:
        for path in ("private/KEYS/a", "src/.Git/config", "a/test.PEM", "a/.env.dev"):
            with pytest.raises(ReadToolError, match="path_denied"):
                workspace.parts(path)
        assert workspace.parts("private/keys-public/a") == ("private", "keys-public", "a")


def test_deadline_and_stop_are_distinct(tmp_path):
    from harnessix.agent.cancellation import TurnCancelled

    with Workspace(tmp_path) as workspace:
        operation = ReadOperation()
        operation.deadline = 0
        with pytest.raises(ReadToolError, match="timeout"):
            with workspace.open(".", operation, directory=True):
                pytest.fail("过期操作不得执行")
        operation.stopped.set()
        with pytest.raises(TurnCancelled):
            operation.checkpoint()
