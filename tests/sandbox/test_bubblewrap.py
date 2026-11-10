"""bubblewrap参数翻译与启动准备（ADR 0111第2.3节）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.processes.supervision_contracts import ProcessSpec
from harnessix.sandbox.bubblewrap import (
    PROCESS_ARGV_LIMIT,
    BubblewrapMasks,
    bubblewrap_argv,
    denied_path_masks,
    prepare_private_directories,
    workspace_env_files,
)
from harnessix.sandbox.native_contracts import NativeSandboxPolicy
from harnessix.sandbox.native_policy import NativeSandboxFacts, build_native_sandbox_policy


def _policy(root: Path, **overrides: object) -> NativeSandboxPolicy:
    home = root / "home"
    workspace = home / "src/app"
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "backend": "bubblewrap",
        "home": home,
        "workspace": workspace,
        "workspace_mode": "read_write",
        "private_tmp": home / ".cache/harnessix/sandbox/x/tmp",
        "private_cache": home / ".cache/harnessix/sandbox/x/cache",
        "git_directories": (workspace / ".git",),
    }
    return build_native_sandbox_policy(NativeSandboxFacts(**(values | overrides)))  # type: ignore[arg-type]


def _pairs(argv: tuple[str, ...], flag: str, arity: int = 2) -> list[tuple[str, ...]]:
    return [
        tuple(argv[index + 1 : index + 1 + arity])
        for index, item in enumerate(argv)
        if item == flag
    ]


def test_argv_limit_matches_process_spec() -> None:
    assert ProcessSpec.model_fields["argv"].metadata[0].max_length == PROCESS_ARGV_LIMIT


def test_translation_orders_mounts_so_protections_win(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    masks = BubblewrapMasks(directories=(str(tmp_path / "home/.ssh"),), files=())
    argv = bubblewrap_argv(
        policy,
        executable="/usr/bin/bwrap",
        command=("/bin/sh", "-c", "true"),
        masks=masks,
        env_files=(str(Path(policy.workspace) / ".env"),),
        hidden_roots=("/tmp", "/run"),
    )
    assert argv[:6] == (
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
    )
    assert argv[-6:] == ("--chdir", policy.workspace, "--", "/bin/sh", "-c", "true")
    order = [argv.index(item) for item in ("--ro-bind", "--dev", "--tmpfs", "--bind")]
    assert order == sorted(order)
    binds = _pairs(argv, "--bind")
    assert {source for source, _ in binds} == set(policy.writable_roots)
    ro = _pairs(argv, "--ro-bind")
    assert ro[0] == ("/", "/")
    git_index = argv.index(str(Path(policy.workspace) / ".git"))
    assert git_index > max(argv.index(root) for root in policy.writable_roots)
    assert (policy.private_cache + "/.empty", str(tmp_path / "home/.ssh")) in ro
    assert ("/dev/null", str(Path(policy.workspace) / ".env")) in ro
    assert _pairs(argv, "--tmpfs", 1) == [("/tmp",), ("/run",)]


def test_overflow_fails_closed_with_distinct_reasons(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    command = ("/bin/sh", "-c", "true")
    many = BubblewrapMasks(files=tuple(f"/x/{index}" for index in range(60)))
    with pytest.raises(KernelError) as error:
        bubblewrap_argv(policy, executable="/b", command=command, masks=many)
    assert error.value.code == "sandbox_policy_too_large"
    env_files = tuple(f"/w/{index}/.env" for index in range(40))
    with pytest.raises(KernelError) as error:
        bubblewrap_argv(
            policy, executable="/b", command=command, masks=BubblewrapMasks(), env_files=env_files
        )
    assert error.value.code == "workspace_secret_files_unmaskable"


def test_seatbelt_policy_is_rejected(tmp_path: Path) -> None:
    policy = _policy(tmp_path, backend="seatbelt")
    with pytest.raises(KernelError):
        bubblewrap_argv(policy, executable="/b", command=("x",), masks=BubblewrapMasks())


def test_only_existing_denied_paths_are_masked_by_real_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".netrc").write_text("machine x")
    dotfiles = tmp_path / "dotfiles/ssh"
    dotfiles.mkdir(parents=True)
    (home / ".ssh").symlink_to(dotfiles)
    masks = denied_path_masks(_policy(tmp_path))
    assert str((home / ".aws").resolve()) in masks.directories
    assert str(dotfiles.resolve()) in masks.directories
    assert masks.files == (str((home / ".netrc").resolve()),)
    assert not any(".kube" in item for item in masks.directories)


def test_nested_denied_paths_keep_only_the_outermost_mask(tmp_path: Path) -> None:
    """外层遮盖已隐藏内层；再遮盖内层会让bwrap在只读空目录里建挂载点而启动失败。"""
    home = tmp_path / "home"
    runtime = home / ".harnessix/workspaces/abc/runtime"
    runtime.mkdir(parents=True)
    (home / ".harnessix/config.json").write_text("{}")
    elsewhere = tmp_path / "state"
    elsewhere.mkdir()
    policy = _policy(
        tmp_path,
        protected_paths=(runtime, home / ".harnessix/config.json", elsewhere),
    )
    masks = denied_path_masks(policy)
    assert masks.directories == tuple(
        sorted((str((home / ".harnessix").resolve()), str(elsewhere.resolve())))
    )
    assert masks.files == ()


def test_private_tmp_is_emptied_before_each_command(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    prepare_private_directories(policy)
    leftover = Path(policy.private_tmp) / "leftover"
    leftover.write_text("x")
    prepare_private_directories(policy)
    assert list(Path(policy.private_tmp).iterdir()) == []
    assert (Path(policy.private_cache) / ".empty").is_dir()


def _layout(workspace: Path) -> None:
    for relative in (
        ".env",
        "svc/.env.local",
        "svc/.ENV.prod",
        "node_modules/pkg/.env",
        "deep/a/b/c/d/e/f/g/h/i/.env",
    ):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SECRET=1\n")
    (workspace / ".env.d").mkdir()
    (workspace / ".venv-like/.env").mkdir(parents=True)  # 名为.env的虚拟环境目录不遮盖
    (workspace / ".envrc").write_text("use nix\n")


def test_walk_finds_env_files_but_skips_dependencies_and_directories(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _layout(workspace)
    found = workspace_env_files(workspace, git=None)
    assert found == tuple(
        sorted(str(workspace / item) for item in (".env", "svc/.env.local", "svc/.ENV.prod"))
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="需要git")
def test_git_listing_covers_tracked_untracked_and_ignored(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _layout(workspace)
    (workspace / ".gitignore").write_text(".env*\nnode_modules/\n")
    git = Path(shutil.which("git") or "git")
    subprocess.run([str(git), "init", "-q", str(workspace)], check=True)
    found = workspace_env_files(workspace, git=git)
    assert str(workspace / ".env") in found
    assert str(workspace / "svc/.env.local") in found
    assert str(workspace / "deep/a/b/c/d/e/f/g/h/i/.env") in found
    assert not any("node_modules" in item for item in found)
    assert not any(item.endswith(".envrc") for item in found)
