from __future__ import annotations

import os

import pytest

from harnessix.cli import main
from harnessix.evals import campaign_cli
from harnessix.evals.campaign_execution_contracts import CodingEvalCampaignRunReport
from tests.evals.test_campaign_execution import execution_config

CANARY = "PRIVATE-CAMPAIGN-CANARY"


def invoke(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        main(["coding-eval-campaign", *argv])
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err
    return raised.value.code, output


def test_campaign_cli_help_is_discoverable(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    assert "coding-eval-campaign" in capsys.readouterr().out
    code, output = invoke(["--help"], capsys)
    assert code == 0 and "--allow-network" in output.out


def test_campaign_cli_disabled_does_not_read_config(capsys, monkeypatch) -> None:
    def forbidden(_):
        pytest.fail("禁网时不得读取配置")

    monkeypatch.setattr(campaign_cli, "_read_config", forbidden)
    code, output = invoke(["--config", CANARY], capsys)
    report = CodingEvalCampaignRunReport.model_validate_json(output.out)
    assert code == 2 and report.reason == "network_not_enabled"


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
def test_campaign_cli_parse_errors_do_not_echo_input(args, capsys) -> None:
    code, output = invoke(args, capsys)
    assert code == 2 and not output.out
    assert output.err == "coding-eval-campaign 参数无效；使用 --help 查看格式。\n"


@pytest.mark.parametrize("mode", [0o644, 0o400])
def test_campaign_cli_requires_private_config(mode, tmp_path, capsys) -> None:
    path = tmp_path / CANARY
    path.write_text("{}", encoding="utf-8")
    path.chmod(mode)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert code == 2
    assert CodingEvalCampaignRunReport.model_validate_json(output.out).reason == (
        "configuration_invalid"
    )


def test_campaign_cli_emits_only_whitelisted_result(tmp_path, capsys, monkeypatch) -> None:
    config = execution_config(tmp_path)
    path = tmp_path / "campaign.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    path.chmod(0o600)
    expected = CodingEvalCampaignRunReport(
        reason="completed",
        campaign_id=config.plan.campaign_id,
        scheduled_trials=2,
        completed_trials=2,
        report_published=True,
        known_cost_currency="CNY",
        known_cost_amount="0.1",
    )

    async def fake_run(checked, *, allow_network):
        assert checked == config and allow_network is True
        return expected

    monkeypatch.setattr(campaign_cli, "run_coding_eval_campaign", fake_run)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert code == 0 and not output.err
    assert CodingEvalCampaignRunReport.model_validate_json(output.out) == expected


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "symlink"])
def test_campaign_cli_rejects_nonregular_config(kind, tmp_path, capsys) -> None:
    path = tmp_path / CANARY
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("{}", encoding="utf-8")
        path.symlink_to(target)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert code == 2
    assert CodingEvalCampaignRunReport.model_validate_json(output.out).reason == (
        "configuration_invalid"
    )


@pytest.mark.parametrize(
    "body",
    [
        b'{"duplicate":1,"duplicate":2}',
        b'{"non_finite":NaN}',
        b"\xff",
        b"x" * (512 * 1024 + 1),
        b"",
    ],
)
def test_campaign_cli_rejects_ambiguous_or_oversized_config(body, tmp_path, capsys) -> None:
    path = tmp_path / CANARY
    path.write_bytes(body)
    path.chmod(0o600)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert code == 2
    assert CodingEvalCampaignRunReport.model_validate_json(output.out).reason == (
        "configuration_invalid"
    )
