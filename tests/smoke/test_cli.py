from __future__ import annotations

import json
import logging
import os

import pytest

from harnessix.cli import main
from harnessix.smoke import cli, runner
from harnessix.smoke.contracts import SmokeReport
from tests.smoke.helpers import CANARY, WireFactory, config


def invoke(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        main(["model-smoke", *argv])
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err
    return raised.value.code, output


def test_cli_help_is_discoverable_and_sdk_free(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    assert "model-smoke" in capsys.readouterr().out
    code, output = invoke(["--help"], capsys)
    assert code == 0 and "--allow-network" in output.out


def test_disabled_does_not_read_config_or_action_plane_env(capsys, monkeypatch):
    monkeypatch.setenv("HARNESSIX_LOG_LEVEL", CANARY)
    monkeypatch.setenv("HARNESSIX_DATABASE_URL", CANARY)

    def forbidden(_):
        pytest.fail("禁止读取配置")

    monkeypatch.setattr(cli, "_read_config", forbidden)
    code, output = invoke(["--config", CANARY], capsys)
    report = SmokeReport.model_validate_json(output.out)
    assert code == 2 and report.reason == "network_not_enabled" and report.attempts_started == 0


@pytest.mark.parametrize(
    "args",
    [
        ["--api-key", CANARY],
        [CANARY],
        ["--config"],
        ["--config", CANARY, "--allow-network=SECRET"],
        ["--conf", CANARY],
    ],
)
def test_cli_parse_errors_never_echo_input(args, capsys):
    code, output = invoke(args, capsys)
    assert code == 2 and not output.out
    assert output.err == "model-smoke 参数无效；使用 --help 查看格式。\n"


@pytest.mark.parametrize(
    "raw",
    [
        CANARY.encode(),
        b'{"provider": "openai_chat", "provider": "anthropic"}',
        b'{"timeout_seconds": NaN}',
        b'{"timeout_seconds": Infinity}',
        b"[" * 2000,
        b"x" * 16385,
        b"\xff",
        b"[]",
        b"null",
    ],
)
def test_bad_config_never_echoes_content(raw, tmp_path, capsys):
    path = tmp_path / CANARY
    path.write_bytes(raw)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert code == 2 and not output.err
    assert SmokeReport.model_validate_json(output.out).reason == "configuration_invalid"


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo"])
def test_nonregular_config_rejected_without_blocking(kind, tmp_path, capsys):
    path = tmp_path / CANARY
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert (
        code == 2 and SmokeReport.model_validate_json(output.out).reason == "configuration_invalid"
    )


@pytest.mark.parametrize("provider", ["openai_chat", "anthropic"])
@pytest.mark.parametrize("scenario", ["text", "tool", "approval"])
def test_cli_actual_sdk_offline_success_with_logging_disabled(
    provider, scenario, tmp_path, capsys, monkeypatch
):
    cfg = config(provider, scenario=scenario)
    path = tmp_path / "config.json"
    path.write_text(cfg.model_dump_json())
    factory = WireFactory()
    monkeypatch.setenv(cfg.api_key_env, CANARY)
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    previous = logging.root.manager.disable

    def substitute(checked):
        assert logging.root.manager.disable == logging.CRITICAL
        logging.getLogger("smoke.test").critical(CANARY)
        return factory(checked)

    monkeypatch.setattr(runner, "_sdk_provider", substitute)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert code == 0 and not output.err
    report = SmokeReport.model_validate_json(output.out)
    assert report.reason == "passed"
    assert logging.root.manager.disable == previous
    assert factory.closed and factory.sdk._client.is_closed()


@pytest.mark.parametrize(
    "error,expected,exit_code",
    [(KeyboardInterrupt(), "cancelled", 130), (RuntimeError(CANARY), "internal_error", 1)],
)
def test_cli_exception_cancellation_redacted_and_logging_restored(
    error, expected, exit_code, tmp_path, capsys, monkeypatch
):
    path = tmp_path / "config.json"
    path.write_text(config().model_dump_json())
    previous = logging.root.manager.disable

    async def fake_run(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli, "run_smoke", fake_run)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert code == exit_code
    assert logging.root.manager.disable == previous
    report = json.loads(output.out)
    assert report["reason"] == expected
    assert report["attempts_started"] is None and report["known_input_tokens"] is None


def test_cli_missing_credentials_does_not_try_network(tmp_path, capsys, monkeypatch):
    cfg = config()
    path = tmp_path / "config.json"
    path.write_text(cfg.model_dump_json())
    monkeypatch.delenv(cfg.api_key_env, raising=False)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert (
        code == 1 and SmokeReport.model_validate_json(output.out).reason == "configuration_invalid"
    )
