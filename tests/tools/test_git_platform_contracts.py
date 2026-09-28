from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.git_read_windows import _environment, _git_read_plan, _GitReadConfiguration
from harnessix.processes.supervision_planner import (
    build_host_process_binding,
    build_process_capability,
    build_process_spec,
)
from harnessix.tools.git import repository_root_matches
from harnessix.tools.runtime import CodingToolRuntime
from tests.tools.test_files import call, execute
from tests.tools.test_git import _command, _git, _repository


def test_windows_read_plan_contract_with_simulated_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 只验证正式DTO和存储，不把模拟Snapshot计作原生Windows证明。
    observation = SimpleNamespace(
        kind="directory",
        identity=("simulated-volume", "simulated-file"),
        content=b"[]",
        size=0,
        entries=(),
    )
    root = SimpleNamespace(
        path=tmp_path,
        root_identity=("simulated-volume", "simulated-file"),
        observe=lambda _path, **_kwargs: observation,
        close=lambda: None,
    )
    monkeypatch.setattr("harnessix.workspace.snapshot._open_native", lambda _path, _platform: root)
    capability = build_process_capability(
        platform="windows",
        supports_pty=False,
        implementation_digest="1" * 64,
    )
    executable = tmp_path / "unused-git.exe"
    config = _GitReadConfiguration(
        tmp_path, executable, tmp_path / "state", capability, "2" * 64, None
    )
    spec = build_process_spec(invocation="argv", argv=(str(executable), "status"))
    environment = _environment(executable)
    plan = _git_read_plan(config, spec, environment)
    assert plan.intent.effect_class.value == "read_only"
    assert plan.intent.source_id == "harnessix.git_read"
    binding = build_host_process_binding(plan, spec, capability, environment, None)
    assert binding.process_spec_digest == spec.digest
    with SQLiteExecutionPlanStore(tmp_path / "plans.db") as plans:
        plans.save_plan(plan)
        assert plans.load_plan(plan.plan_id) == plan


@pytest.mark.parametrize(
    ("observed", "expected", "windows", "matches"),
    [
        ("C:/work/工程\n", "c:\\work\\工程", True, True),
        ("C:/work/工程\r\n", "c:\\work\\工程", True, True),
        ("C:/work/工程\n\r\n", "c:\\work\\工程", True, False),
        ("C:/work/工程\n\n", "c:\\work\\工程", True, False),
        ("C:/work/工程\0\n", "c:\\work\\工程", True, False),
        ("C:/work\n", "c:\\work\\工程", True, False),
        ("C:work/工程\n", "c:\\work\\工程", True, False),
        ("/work/Root\n", "/work/root", False, False),
        ("/work/root\n", "/work/root", False, True),
        ("/work/root\r\n", "/work/root", False, False),
    ],
)
def test_repository_root_comparison_is_platform_specific(
    observed: str,
    expected: str,
    windows: bool,
    matches: bool,
) -> None:
    assert repository_root_matches(observed, expected, windows=windows) is matches


@pytest.mark.skipif(os.name != "posix", reason="POSIX绑定漂移，Windows由原生案例验证")
async def test_git_rejects_executable_replacement_after_catalog_binding(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    executable = tmp_path / "git-copy"
    shutil.copyfile(_git(), executable)
    executable.chmod(0o700)
    async with CodingToolRuntime(root, git_executable=executable) as tools:
        executable.write_bytes(b"replaced executable, must not run")
        with pytest.raises(KernelError, match="Git只读宿主绑定已变化") as changed:
            await tools.execute(call(tools, "git_status"), CancelToken())
        assert changed.value.code == "process_binding_changed"


@pytest.mark.skipif(os.name != "posix", reason="真实POSIX Git辅助程序防护")
@pytest.mark.parametrize("kind", ["clean", "process", "include", "includeif"])
async def test_git_rejects_executable_filters_and_configuration_includes(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _repository(root)
    marker = tmp_path / "unexpected-helper"
    helper = tmp_path / "filter.py"
    helper.write_text(
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('executed'); "
        "sys.stdout.buffer.write(sys.stdin.buffer.read())",
        encoding="utf-8",
    )
    command = shlex.join((sys.executable, "-I", str(helper), str(marker)))
    if kind in {"clean", "process"}:
        _command(root, "config", f"filter.fixture.{kind}", command)
        (root / ".gitattributes").write_bytes(b"tracked.py filter=fixture\n")
    else:
        key = "include.path" if kind == "include" else "includeIf.gitdir:/**.path"
        _command(root, "config", key, str(tmp_path / "external-config"))
    (root / "tracked.py").write_bytes(b"after\n")
    if kind == "clean":
        # 正向对照证明Git Diff确会触发clean，而不是无效辅助程序夹具。
        _command(root, "-c", "core.fsmonitor=false", "diff", "--no-ext-diff", "--no-textconv", "--")
        assert marker.read_text() == "executed"
        marker.unlink()
    async with CodingToolRuntime(root, git_executable=_git()) as tools:
        result = await execute(tools, "git_diff")
    assert result.error.code == "tool_path_denied"
    assert not marker.exists()
