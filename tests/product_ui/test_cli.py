from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from harnessix import cli
from harnessix.product_ui import cli as product_cli
from tests.product_config.conftest import product_config, write_config


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


def test_product_command_forwards_action_config_and_activation_preconditions(
    tmp_path: Path,
) -> None:
    command = product_cli._server_command(
        config=tmp_path / "config.json",
        action_config=tmp_path / "actions.json",
        profile="primary",
        workspace=tmp_path,
        runtime_state=tmp_path / "runtime",
        git_executable=None,
        expected_active_sha256="a" * 64,
        expected_active_profile="previous",
        expected_active_action_sha256="b" * 64,
    )

    assert command[-8:] == (
        "--action-config",
        str(tmp_path / "actions.json"),
        "--expected-active-sha256",
        "a" * 64,
        "--expected-active-profile",
        "previous",
        "--expected-active-action-sha256",
        "b" * 64,
    )


def test_code_cli_rejects_missing_workspace_with_stable_redacted_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        product_cli,
        "_product_app_type",
        lambda: pytest.fail("Preflight失败前不得加载TUI"),
    )
    secret = "secret-path-canary"

    with pytest.raises(SystemExit) as error:
        product_cli.code_main([str(tmp_path / secret)])

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "Harnessix Code检查结论：不可启动" in captured.err
    assert "workspace_binding_invalid" in captured.err
    assert secret not in captured.err and captured.out == ""


def test_code_configure_creates_non_secret_v2_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "api-key-must-not-enter-configure"
    monkeypatch.setenv("PRIMARY_API_KEY", canary)
    path = tmp_path / "private" / "config.json"

    product_cli.code_main(
        [
            "configure",
            "--config",
            str(path),
            "--provider-kind",
            "openai_chat",
            "--base-url",
            "https://api.openai.test/v1",
            "--model",
            "gpt-test",
            "--api-key-env",
            "PRIMARY_API_KEY",
            "--non-interactive",
        ]
    )

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["operation"] == "created"
    assert path.exists()
    assert canary not in path.read_text(encoding="utf-8")
    assert canary not in json.dumps(receipt)


def test_code_configure_non_interactive_missing_fields_is_stable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        product_cli.code_main(
            ["configure", "--config", str(tmp_path / "config.json"), "--non-interactive"]
        )
    assert error.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "product_configure_arguments_required"


def test_code_configure_requires_explicit_replace_and_exact_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config.json"
    base = [
        "configure",
        "--config",
        str(path),
        "--provider-kind",
        "openai_chat",
        "--base-url",
        "https://api.openai.test/v1",
        "--model",
        "gpt-test",
        "--non-interactive",
    ]
    product_cli.code_main(base)
    created = json.loads(capsys.readouterr().out)

    with pytest.raises(SystemExit) as implicit_replace:
        product_cli.code_main([*base, "--expected-source-sha256", created["source_sha256"]])
    assert implicit_replace.value.code == 2
    assert json.loads(capsys.readouterr().err)["code"] == "product_config_write_invalid"

    product_cli.code_main(
        [
            *base[:-2],
            "gpt-next",
            "--non-interactive",
            "--replace",
            "--expected-source-sha256",
            created["source_sha256"],
        ]
    )
    replaced = json.loads(capsys.readouterr().out)
    assert replaced["operation"] == "replaced"
    assert replaced["previous_source_sha256"] == created["source_sha256"]


def test_code_doctor_json_uses_shared_report_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = product_config()
    path = write_config(tmp_path / "config.json", config)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("PRIMARY_API_KEY", "primary-secret")
    monkeypatch.setenv("BACKUP_API_KEY", "backup-secret")

    with pytest.raises(SystemExit) as completed:
        product_cli.code_main(
            [
                "doctor",
                str(workspace),
                "--config",
                str(path),
                "--state-directory",
                str(state),
                "--json",
            ]
        )
    assert completed.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is True and report["mode"] == "doctor"
    assert report["platform"] == ("windows" if os.name == "nt" else "posix")
    assert not state.exists()
