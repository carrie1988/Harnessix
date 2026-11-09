"""零配置命令工具（ADR 0111第2.2节、ADR 0113第2.3节）与评测启动不受影响。"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.context.contracts import ContextBuildInput
from harnessix.product_config.action_codec import load_product_action_config
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.product_config.zero_config import (
    ZERO_CONFIG_ENVIRONMENT,
    zero_config_action_config,
    zero_config_shell,
)
from harnessix.sandbox.native_contracts import HostSandboxProbeV2
from harnessix.sandbox.native_probe import probe_native_sandbox

pytestmark = pytest.mark.skipif(os.name != "posix", reason="零配置命令只在POSIX平台生成")


def _probe(tmp_path: Path, code: int):
    executable = tmp_path / "bwrap"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        output = "bubblewrap 0.6.1\n" if "--version" in argv else ""
        return subprocess.CompletedProcess(argv, 0 if "--version" in argv else code, output, "")

    def probe() -> HostSandboxProbeV2:
        return probe_native_sandbox(
            platform="linux", runner=run, bubblewrap_candidates=(executable,)
        )

    return probe


def test_sandbox_available_yields_sandboxed_and_host_commands(tmp_path: Path) -> None:
    config = zero_config_action_config({"SHELL": "/bin/sh"}, probe=_probe(tmp_path, 0))
    profiles = {item.profile_id: item for item in config.native_process_profiles}
    assert [item.profile_id for item in config.native_process_profiles] == ["host-shell", "shell"]
    assert profiles["shell"].backend == "bubblewrap"
    assert profiles["shell"].workspace_mode == "read_write"
    assert profiles["host-shell"].backend == "host"
    assert config.workspace_patch_enabled


def test_sandbox_unavailable_yields_only_host_command_with_reason(tmp_path: Path) -> None:
    config = zero_config_action_config({}, probe=_probe(tmp_path, 1))
    (profile,) = config.native_process_profiles
    assert profile.backend == "host"
    assert "本机沙箱不可用（bubblewrap_preflight_failed）" in profile.description


def test_shell_follows_login_shell_only_when_supported(tmp_path: Path) -> None:
    assert zero_config_shell({"SHELL": "/usr/bin/fish"}) == "/bin/sh"
    assert zero_config_shell({"SHELL": "relative/zsh"}) == "/bin/sh"
    assert zero_config_shell({}) == "/bin/sh"
    for candidate in ("/bin/zsh", "/bin/bash"):
        if Path(candidate).is_file():
            assert zero_config_shell({"SHELL": candidate}) == candidate


def test_omitted_config_keeps_old_builtin_unless_explicitly_requested() -> None:
    """评测与程序化启动不请求零配置，内建配置与原来逐字相同。"""

    assert load_product_action_config(None).config == build_product_action_config()
    requested = load_product_action_config(None, zero_config=True, environ={"SHELL": "/bin/sh"})
    assert requested.source_kind == "builtin"
    assert requested.config.native_process_profiles


async def _runtime_instruction(tmp_path: Path, **options: bool) -> dict[str, str]:
    context = build_product_agent_context(tmp_path, max_output_tokens=1000, **options)
    request = ContextBuildInput(
        thread_id=uuid4(), turn_id=uuid4(), model_step=1, workspace=str(tmp_path)
    )
    prepared = await context.context.prepare(request, CancelToken())
    fragments = json.loads(prepared.instructions or "")["fragments"]
    (runtime,) = [item for item in fragments if item["kind"] == "runtime_instruction"]
    return runtime


async def test_native_instructions_are_separate_from_generic_ones(tmp_path: Path) -> None:
    generic = await _runtime_instruction(tmp_path, generic_execution=True)
    native = await _runtime_instruction(tmp_path, generic_execution=True, native_execution=True)
    assert generic["source"] == "harnessix.generic-execution-instructions/v4"
    assert native["source"] == "harnessix.native-execution-instructions/v3"
    assert "Inside execution containers" in generic["content"]
    assert "host_command.<profile>" in native["content"]
    assert "Inside execution containers" not in native["content"]
    with pytest.raises(ValueError):
        build_product_agent_context(tmp_path, max_output_tokens=1000, native_execution=True)


def test_user_entry_requests_zero_config_explicitly_without_touching_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import argparse

    from harnessix.product_ui import cli

    def request(action_config: str | None):
        arguments = argparse.Namespace(
            config=str(tmp_path / "config.json"),
            action_config=action_config,
            profile=None,
            workspace=str(tmp_path),
            state_directory=str(tmp_path / "state"),
            git_executable=None,
        )
        return cli._preflight_request(arguments, mode="doctor", require_tui=False)

    def server(action_config: Path | None) -> tuple[str, ...]:
        return cli._server_command(
            config=tmp_path / "config.json",
            action_config=action_config,
            profile=None,
            workspace=tmp_path,
            runtime_state=tmp_path / "state",
            git_executable=None,
        )

    monkeypatch.delenv(ZERO_CONFIG_ENVIRONMENT, raising=False)
    monkeypatch.delenv("HARNESSIX_PRODUCT_ACTION_CONFIG", raising=False)
    before = dict(os.environ)
    assert request(None).zero_config_actions
    assert "--zero-config-actions" in server(None)
    assert not request(str(tmp_path / "actions.json")).zero_config_actions
    assert "--zero-config-actions" not in server(tmp_path / "actions.json")
    assert dict(os.environ) == before
    monkeypatch.setenv(ZERO_CONFIG_ENVIRONMENT, "0")
    assert not request(None).zero_config_actions
    assert "--zero-config-actions" not in server(None)
