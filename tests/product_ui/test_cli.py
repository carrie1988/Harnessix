from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harnessix import cli
from harnessix.product_ui import cli as product_cli


def test_top_level_code_command_delegates_without_loading_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    monkeypatch.setattr(product_cli, "code_main", lambda args: captured.append(list(args)))

    cli.main(["code", "/workspace", "--profile", "primary"])

    assert captured == [["/workspace", "--profile", "primary"]]


def test_product_command_builds_current_python_stdio_server_argv(tmp_path: Path) -> None:
    command = product_cli._server_command(
        config=tmp_path / "config.json",
        profile="primary",
        workspace=tmp_path,
        runtime_state=tmp_path / "state" / "runtime",
        git_executable="/usr/bin/git",
    )

    assert command == (
        sys.executable,
        "-m",
        "harnessix",
        "agent-server",
        "--config",
        str(tmp_path / "config.json"),
        "--workspace",
        str(tmp_path),
        "--state-directory",
        str(tmp_path / "state" / "runtime"),
        "--profile",
        "primary",
        "--git-executable",
        "/usr/bin/git",
    )


def test_code_cli_rejects_missing_workspace_with_stable_redacted_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnusedApp:
        pass

    monkeypatch.setattr(product_cli, "_product_app_type", lambda: UnusedApp)
    secret = "secret-path-canary"

    with pytest.raises(SystemExit) as error:
        product_cli.code_main([str(tmp_path / secret)])

    assert error.value.code == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload == {
        "code": "product_workspace_invalid",
        "message": "产品Workspace不可用",
        "retryable": False,
    }
    assert secret not in captured.err and captured.out == ""
