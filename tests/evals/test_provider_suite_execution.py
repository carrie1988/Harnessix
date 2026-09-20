from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals import provider_suite_execution
from harnessix.evals.provider_suite_execution import run_task_pack_provider_suite
from harnessix.evals.suite_execution_contracts import (
    CodingEvalSuiteCaseRunResult,
    CodingEvalSuiteRunReport,
)
from tests.evals.provider_suite_helpers import provider_suite_config


async def test_provider_suite_is_fail_closed_before_scope_or_secret_access(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = provider_suite_config(tmp_path)

    def forbidden(*_args):
        pytest.fail("默认禁网不得核验宿主或创建Provider")

    monkeypatch.setattr(provider_suite_execution, "_require_scope", forbidden)
    with pytest.raises(KernelError) as raised:
        await run_task_pack_provider_suite(config)
    assert raised.value.code == "eval_provider_suite_network_disabled"


async def test_provider_suite_reuses_suite_runner_with_config_fingerprint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = provider_suite_config(tmp_path)

    async def executor(case, campaign, case_root, cancel):
        return CodingEvalSuiteCaseRunResult(case_id=case.case_id, reason="runtime_failed")

    def scope(checked, observability):
        assert checked == config and observability is None
        return executor

    async def run(suite, checked_executor, **kwargs):
        assert suite == config.suite and checked_executor is executor
        assert kwargs["execution_binding_sha256"] == config.fingerprint
        assert kwargs["resume"] is True
        return CodingEvalSuiteRunReport(
            reason="completed",
            suite_id=suite.plan.suite_id,
            scheduled_cases=10,
            completed_cases=10,
            report_published=True,
            known_cost_currency="CNY",
            known_cost_amount="1",
        )

    monkeypatch.setattr(provider_suite_execution, "_require_scope", scope)
    monkeypatch.setattr(provider_suite_execution, "run_coding_eval_suite", run)

    result = await run_task_pack_provider_suite(config, allow_network=True, resume=True)
    assert result.reason == "completed"


def test_provider_suite_source_revision_mismatch_is_stable_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = provider_suite_config(tmp_path)

    class Completed:
        returncode = 0
        stdout = b"f" * 40 + b"\n"

    monkeypatch.setattr(provider_suite_execution.subprocess, "run", lambda *a, **k: Completed())
    with pytest.raises(KernelError) as raised:
        provider_suite_execution._require_source_revision(config, Path("/usr/bin/git"))
    assert raised.value.code == "eval_provider_suite_source_revision_mismatch"
