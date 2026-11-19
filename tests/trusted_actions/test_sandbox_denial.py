"""沙箱拦截的提示只在本机沙箱、有明确拦截迹象时出现（ADR 0176，Beta Issue #44后续）。

0.10.0b55：Maven Wrapper首次运行在创建~/.m2时被Seatbelt拦下，模型只得到普通的非零退出说明。
判断是纯函数，三个平台都运行；真实沙箱里的端到端验证见tests/product_config/test_native_sandbox_action.py。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from harnessix.trusted_actions.sandbox_denial import (
    DENIAL_SIGNATURES,
    SANDBOX_DENIAL_HINT,
    sandbox_denial_hint,
)


def _plan(level: str) -> Any:
    return cast(
        Any, SimpleNamespace(execution=SimpleNamespace(sandbox=SimpleNamespace(level=level)))
    )


def _output(stdout: str | None = "", stderr: str | None = "") -> dict[str, Any]:
    return {
        "version": "trusted-process-output/v4",
        "diagnostic_preview": {"stdout": {"text": stdout}, "stderr": {"text": stderr}},
    }


@pytest.mark.parametrize("signature", DENIAL_SIGNATURES)
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_a_sandboxed_command_with_a_denial_gets_the_hint(signature: str, stream: str) -> None:
    text = f"error: step failed: {signature.upper()} (while preparing)\n"
    output = _output(**{stream: text})
    assert sandbox_denial_hint(_plan("host_sandboxed"), output) == SANDBOX_DENIAL_HINT


def test_the_maven_wrapper_cold_start_from_the_report_gets_the_hint() -> None:
    stderr = "mkdir: /Users/me/.m2: Operation not permitted\n"
    assert sandbox_denial_hint(_plan("host_sandboxed"), _output(stderr=stderr)) is not None


@pytest.mark.parametrize(
    "stderr",
    [
        "AssertionError: expected 2 but got 3\n",
        "bash: ./run.sh: Permission denied\n",
        "requests.exceptions.ConnectionError: Connection refused\n",
        "",
    ],
)
def test_ordinary_failures_get_no_hint(stderr: str) -> None:
    assert sandbox_denial_hint(_plan("host_sandboxed"), _output(stderr=stderr)) is None


@pytest.mark.parametrize("level", ["host_guarded", "container_strong"])
def test_commands_outside_the_local_sandbox_get_no_hint(level: str) -> None:
    stderr = "mkdir: /home/me/.m2: Operation not permitted\n"
    assert sandbox_denial_hint(_plan(level), _output(stderr=stderr)) is None


@pytest.mark.parametrize(
    "output",
    [
        None,
        {"version": "trusted-process-output/v1"},
        {"diagnostic_preview": {"stdout": None, "stderr": {"text": None}}},
        {"diagnostic_preview": {"stdout": "Operation not permitted", "stderr": []}},
    ],
)
def test_missing_or_hidden_previews_get_no_hint(output: Any) -> None:
    assert sandbox_denial_hint(_plan("host_sandboxed"), output) is None
