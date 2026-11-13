"""产品自己创建的状态目录，每一级都只允许当前用户访问。

0.10.0b24上的实测：还没运行过`harnessix code configure`就先用`harnessix exec --config <别处>`，
`~/.harnessix`与`~/.harnessix/workspaces`被建成755（只有最里层是700），之后`configure`拒绝这个目录。
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessix.product_config.private_directory import make_private_directories
from harnessix.product_config.state_owner import product_state_owner
from harnessix.product_ui import ClientStateStore
from harnessix.product_ui import cli as product_cli

posix_only = pytest.mark.skipif(os.name != "posix", reason="目录权限位只在POSIX上有意义")


@pytest.fixture
def default_umask() -> Iterator[None]:
    """常见的umask 022：不指定权限时目录建成755。"""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


@posix_only
def test_every_missing_level_is_created_private(tmp_path: Path, default_umask: None) -> None:
    leaf = tmp_path / "a" / "b" / "c"

    make_private_directories(leaf)

    assert [_mode(level) for level in (leaf.parent.parent, leaf.parent, leaf)] == [0o700] * 3


@posix_only
def test_existing_directories_keep_their_mode(tmp_path: Path, default_umask: None) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)

    make_private_directories(shared / "state" / "runtime")
    make_private_directories(shared)

    assert _mode(shared) == 0o755
    assert _mode(shared / "state") == _mode(shared / "state" / "runtime") == 0o700


def test_a_file_on_the_path_is_an_error(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError):
        make_private_directories(occupied)
    with pytest.raises(OSError):
        make_private_directories(occupied / "below")


@posix_only
def test_exec_before_configure_leaves_a_home_that_configure_accepts(
    tmp_path: Path, default_umask: None
) -> None:
    """服务端启动时准备状态目录的上一级；这是先用exec的用户第一次创建`~/.harnessix`的地方。"""
    private_home = _home(tmp_path) / ".harnessix"
    state_root = private_home / "workspaces" / "fingerprint" / "runtime"

    with product_state_owner(state_root):
        pass

    assert _mode(private_home) == _mode(private_home / "workspaces") == 0o700
    product_cli.code_main(
        [
            "configure",
            "--config",
            str(private_home / "config.json"),
            "--vendor",
            "bailian",
            "--non-interactive",
        ]
    )
    assert (private_home / "config.json").is_file()


@posix_only
def test_the_terminal_client_creates_its_state_levels_private(
    tmp_path: Path, default_umask: None
) -> None:
    private_home = _home(tmp_path) / ".harnessix"
    client_state = private_home / "workspaces" / "fingerprint"

    with ClientStateStore(client_state, workspace_identity=str(tmp_path / "workspace")):
        pass

    assert [_mode(level) for level in (private_home, client_state.parent, client_state)] == [
        0o700
    ] * 3
