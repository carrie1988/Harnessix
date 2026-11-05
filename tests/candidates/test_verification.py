from __future__ import annotations

import subprocess
from pathlib import Path

from harnessix.candidates.selection import Check
from harnessix.candidates.verification import (
    container_argv,
    cross_check,
    overlay_workspace,
    scratch_files,
)
from harnessix.product_config.action_contracts import build_product_process_profile

IMAGE = "sha256:" + "1" * 64


def _profile(**overrides: object):
    values: dict[str, object] = {
        "profile_id": "testbed",
        "version": "v1",
        "description": "candidate checks",
        "container_engine": "/usr/bin/docker",
        "image": IMAGE,
        "program": "/bin/bash",
        "arguments": ("-c",),
        "selector_policy": "shell_command",
        "timeout_seconds": 60,
        "workspace_path": "/testbed",
    }
    return build_product_process_profile(**(values | overrides))


def test_container_argv_keeps_the_profile_isolation(tmp_path: Path) -> None:
    argv = container_argv(_profile(), tmp_path, "python -m pytest -q")
    joined = " ".join(argv)
    for flag in ("--read-only", "--cap-drop ALL", "--network none", "--user 65532:65532"):
        assert flag in joined
    assert f"type=bind,src={tmp_path},dst=/testbed,readonly" in argv
    assert argv[argv.index("--workdir") + 1] == "/testbed"
    assert argv[-3:] == [IMAGE, "-c", "python -m pytest -q"]
    default_path = container_argv(_profile(workspace_path=None), tmp_path, "true")
    assert f"type=bind,src={tmp_path},dst=/workspace,readonly" in default_path


def _workspace(root: Path, name: str, scratch: str | None = None) -> Path:
    workspace = root / name
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "mod.py").write_text(f"VALUE = {name!r}\n", encoding="utf-8")
    if scratch is not None:
        (workspace / "harnessix_scratch").mkdir()
        (workspace / "harnessix_scratch" / "repro.py").write_text(scratch, encoding="utf-8")
    return workspace


def test_overlay_adds_other_candidates_scratch_without_touching_the_original(
    tmp_path: Path,
) -> None:
    target = _workspace(tmp_path, "b", scratch="print('b')\n")
    source = _workspace(tmp_path, "a", scratch="print('a')\n")
    assert scratch_files(source) == [Path("harnessix_scratch/repro.py")]

    with overlay_workspace(target, source, tmp_path / "copy") as merged:
        assert (merged / "harnessix_scratch" / "repro.py").read_text() == "print('a')\n"
        assert (merged / "pkg" / "mod.py").read_text() == "VALUE = 'b'\n"
    assert (target / "harnessix_scratch" / "repro.py").read_text() == "print('b')\n"
    assert not (tmp_path / "copy").exists()


def test_cross_check_runs_every_check_on_every_candidate(tmp_path: Path) -> None:
    workspaces = {
        "a": _workspace(tmp_path, "a", scratch="a"),
        "b": _workspace(tmp_path, "b"),
    }
    seen: list[tuple[str, str]] = []

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        mount = argv[argv.index("--mount") + 1]
        source = Path(mount.split(",")[1].removeprefix("src="))
        seen.append((source.name, argv[-1]))
        code = 0 if (source / "harnessix_scratch").exists() else 1
        return subprocess.CompletedProcess(argv, code, b"out", b"")

    checks = (Check("python harnessix_scratch/repro.py", "a"),)
    runs = cross_check(_profile(), workspaces, checks, tmp_path / "scratch", runner=runner)

    results = {run.candidate_id: run.result for run in runs}
    # b上运行a的复现脚本时叠加了a的草稿文件。
    assert results == {"a": "passed", "b": "passed"}
    assert sorted(name for name, _ in seen) == ["a", "b-check0"]


def test_container_engine_errors_are_not_counted_as_test_failures(tmp_path: Path) -> None:
    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 125, b"", b"no such image")

    runs = cross_check(
        _profile(),
        {"a": _workspace(tmp_path, "a")},
        (Check("pytest", "a"),),
        tmp_path / "s",
        runner=runner,
    )
    assert [run.result for run in runs] == ["error"]


def test_snapshot_profiles_check_on_private_writable_copies(tmp_path: Path) -> None:
    profile = _profile(workspace_mode="snapshot", environment=(("HOME", "/tmp"),))
    argv = container_argv(profile, tmp_path, "make test")
    assert f"type=bind,src={tmp_path},dst=/testbed" in argv
    assert "noexec" not in argv[argv.index("--tmpfs") + 1]
    assert argv[argv.index("--env") + 1] == "HOME=/tmp"

    workspaces = {"a": _workspace(tmp_path, "a")}

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        mount = argv[argv.index("--mount") + 1]
        source = Path(mount.split(",")[1].removeprefix("src="))
        assert source != workspaces["a"]  # 写入落在副本上
        (source / "pkg" / "mod.py").write_text("overwritten\n")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    runs = cross_check(profile, workspaces, (Check("make", "a"),), tmp_path / "s", runner=runner)
    assert [run.result for run in runs] == ["passed"]
    assert (workspaces["a"] / "pkg" / "mod.py").read_text() == "VALUE = 'a'\n"
