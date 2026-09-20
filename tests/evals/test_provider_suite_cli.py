from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnessix.cli import main
from harnessix.domain.models import utc_now
from harnessix.evals import provider_suite_cli
from harnessix.evals.provider_suite_contracts import CodingEvalProviderSuiteRunReport
from harnessix.evals.report import write_eval_suite_execution_state
from harnessix.evals.suite_execution import suite_execution_fingerprint
from harnessix.evals.suite_execution_contracts import (
    CodingEvalSuiteExecutionState,
    CodingEvalSuiteRunReport,
)
from tests.evals.provider_suite_helpers import provider_suite_config

CANARY = "PRIVATE-PROVIDER-SUITE-CANARY"


def invoke(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        main(["coding-eval-suite", *argv])
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err
    return raised.value.code, output


def test_provider_suite_cli_help_is_discoverable(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    assert "coding-eval-suite" in capsys.readouterr().out
    code, output = invoke(["--help"], capsys)
    assert code == 0 and "--allow-network" in output.out and "--resume" in output.out


def test_provider_suite_cli_disabled_does_not_read_config(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_suite_cli,
        "_read_config",
        lambda _: pytest.fail("禁网时不得读取配置"),
    )
    code, output = invoke(["--config", CANARY], capsys)
    report = CodingEvalProviderSuiteRunReport.model_validate_json(output.out)
    assert code == 2 and report.reason == "network_not_enabled"


@pytest.mark.parametrize("mode", [0o644, 0o400])
def test_provider_suite_cli_requires_private_config(mode, tmp_path, capsys) -> None:
    path = tmp_path / CANARY
    path.write_text("{}", encoding="utf-8")
    path.chmod(mode)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    assert code == 2
    assert CodingEvalProviderSuiteRunReport.model_validate_json(output.out).reason == (
        "configuration_invalid"
    )


def test_provider_suite_cli_emits_only_whitelisted_result(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config = provider_suite_config(tmp_path)
    path = tmp_path / "provider-suite.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    path.chmod(0o600)
    expected = CodingEvalSuiteRunReport(
        reason="completed",
        suite_id=config.suite.plan.suite_id,
        scheduled_cases=10,
        completed_cases=10,
        report_published=True,
        known_cost_currency="CNY",
        known_cost_amount="1.5",
    )

    async def fake_run(checked, *, allow_network, resume):
        assert checked == config and allow_network is True and resume is True
        return expected

    monkeypatch.setattr(provider_suite_cli, "run_task_pack_provider_suite", fake_run)
    code, output = invoke(
        ["--config", str(path), "--allow-network", "--resume"],
        capsys,
    )
    report = CodingEvalProviderSuiteRunReport.model_validate_json(output.out)
    assert code == 0 and not output.err
    assert report.reason == "completed" and report.known_cost_amount == "1.5"


def test_provider_suite_cli_runtime_failure_preserves_trusted_progress(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config = provider_suite_config(tmp_path)
    path = tmp_path / "provider-suite.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    path.chmod(0o600)
    work_root = Path(config.suite.work_root)
    work_root.mkdir(mode=0o700)
    case_ids = tuple(case.case_id for case in config.suite.plan.cases)
    now = utc_now()
    write_eval_suite_execution_state(
        work_root / "suite-state.json",
        CodingEvalSuiteExecutionState(
            suite_id=config.suite.plan.suite_id,
            plan_fingerprint=config.suite.plan.fingerprint,
            execution_config_fingerprint=suite_execution_fingerprint(
                config.suite, config.fingerprint
            ),
            status="running",
            completed_case_ids=case_ids[:9],
            current_case_id=case_ids[9],
            known_cost_currency=config.suite.fee_stop_currency,
            known_cost_amount="1.44998",
            started_at=now,
            updated_at=now,
        ),
    )

    async def fail(*_args, **_kwargs):
        raise provider_suite_cli.KernelError("test_failure", CANARY)

    monkeypatch.setattr(provider_suite_cli, "run_task_pack_provider_suite", fail)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    report = CodingEvalProviderSuiteRunReport.model_validate_json(output.out)

    assert code == 1 and not output.err
    assert report.reason == "runtime_failed"
    assert report.completed_cases == 9
    assert report.current_case_id == case_ids[9]
    assert report.known_cost_currency == "CNY"
    assert report.known_cost_amount == "1.44998"


@pytest.mark.parametrize("mismatch", ["plan", "execution_binding"])
def test_provider_suite_cli_ignores_progress_from_another_execution(
    mismatch: str,
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config = provider_suite_config(tmp_path)
    path = tmp_path / "provider-suite.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    path.chmod(0o600)
    work_root = Path(config.suite.work_root)
    work_root.mkdir(mode=0o700)
    now = utc_now()
    write_eval_suite_execution_state(
        work_root / "suite-state.json",
        CodingEvalSuiteExecutionState(
            suite_id=config.suite.plan.suite_id,
            plan_fingerprint=("f" * 64 if mismatch == "plan" else config.suite.plan.fingerprint),
            execution_config_fingerprint=(
                "f" * 64
                if mismatch == "execution_binding"
                else suite_execution_fingerprint(config.suite, config.fingerprint)
            ),
            status="running",
            completed_case_ids=tuple(case.case_id for case in config.suite.plan.cases[:9]),
            known_cost_currency=config.suite.fee_stop_currency,
            known_cost_amount="39",
            started_at=now,
            updated_at=now,
        ),
    )

    async def fail(*_args, **_kwargs):
        raise provider_suite_cli.KernelError("test_failure", CANARY)

    monkeypatch.setattr(provider_suite_cli, "run_task_pack_provider_suite", fail)
    code, output = invoke(["--config", str(path), "--allow-network"], capsys)
    report = CodingEvalProviderSuiteRunReport.model_validate_json(output.out)

    assert code == 1 and report.reason == "runtime_failed"
    assert report.completed_cases == 0
    assert report.current_case_id is None
    assert report.known_cost_amount == "0"


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "symlink"])
def test_provider_suite_cli_rejects_nonregular_config(kind, tmp_path, capsys) -> None:
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
    assert CodingEvalProviderSuiteRunReport.model_validate_json(output.out).reason == (
        "configuration_invalid"
    )
