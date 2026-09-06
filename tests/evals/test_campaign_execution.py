from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import Usage
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.domain.models import ContractModel, utc_now
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.campaign_execution import run_coding_eval_campaign
from harnessix.evals.campaign_execution_contracts import (
    CodingEvalCampaignExecutionState,
    CodingEvalCampaignRunConfig,
    CodingEvalCampaignRunReport,
)
from harnessix.evals.catalog import historical_coding_eval
from harnessix.evals.contracts import CodingEvalEnvironment
from harnessix.evals.report import (
    read_eval_campaign_execution_state,
    read_eval_campaign_plan,
    read_eval_campaign_report,
)
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.pricing import BillingContext, FlatInputPrice, PriceSnapshot
from tests.evals.test_runner import HistoricalFixProvider

ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "harnessix-openai-empty-incremental-call-id"
MODEL = "fixture-coder-v1"


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def exists(path: Path) -> bool:
    return path.exists()


def create_private_roots(root: Path) -> None:
    root.mkdir(mode=0o700)
    (root / "runs").mkdir(mode=0o700)


def git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git不可用")
    return Path(executable).resolve()


def revision() -> str:
    return subprocess.run(
        [str(git_executable()), "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def execution_config(
    tmp_path: Path,
    *,
    runs: int = 2,
    fee_stop_amount: str = "1",
    task_version: int | None = None,
) -> CodingEvalCampaignRunConfig:
    task = historical_coding_eval(TASK_ID, task_version).task
    now = utc_now()
    price = PriceSnapshot(
        version="fixture-2026-09-06",
        source_url="https://pricing.invalid/model",
        billing_provider="fixture-cloud",
        model=MODEL,
        region="fixture-region",
        service_tier="payg",
        inference_mode="non-thinking",
        currency="CNY",
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        input_tokens_min=0,
        input_tokens_max=None,
        input_price=FlatInputPrice(per_million="1"),
        output_per_million="2",
    )
    context = BillingContext(
        billing_provider="fixture-cloud",
        region="fixture-region",
        service_tier="payg",
        inference_mode="non-thinking",
    )
    environment = CodingEvalEnvironment(
        harnessix_revision=revision(),
        provider="openai_chat",
        model=MODEL,
        platform=sys.platform,
        isolation="private-managed-copy-no-os-sandbox",
    )
    plan = CodingEvalCampaignPlan(
        campaign_id=uuid4(),
        task_id=task.task_id,
        task_version=task.task_version,
        task_fingerprint=task.fingerprint,
        environment=environment,
        run_ids=tuple(uuid4() for _ in range(runs)),
        price=price,
        billing_context=context,
        created_at=now,
    )
    return CodingEvalCampaignRunConfig(
        plan=plan,
        source_root=str(ROOT),
        work_root=str(tmp_path / "private-campaign"),
        git_executable=str(git_executable()),
        python_executable=sys.executable,
        provider_config=OpenAIChatConfig(
            base_url="https://provider.invalid/v1",
            model=MODEL,
            api_key_env="FIXTURE_API_KEY",
            capabilities=ChatCapabilities(tool_calls=True, parallel_tool_calls=False),
            max_output_tokens=512,
            max_attempts=1,
            retry_delay_seconds=0,
            output_token_parameter="max_tokens",
        ),
        fee_stop_currency="CNY",
        fee_stop_amount=fee_stop_amount,
    )


class CostedHistoricalFixProvider(HistoricalFixProvider):
    async def stream(self, request, cancel):
        attempt_id = new_id()
        response_id = f"historical-fix-{request.step}"
        usage = Usage(input_tokens=100, output_tokens=5)
        yield ModelAttemptStarted(
            attempt_id=attempt_id,
            step=request.step,
            index=1,
            provider="openai_chat",
            requested_model=MODEL,
        )
        async for event in super().stream(request, cancel):
            if isinstance(event, ResponseCompleted):
                yield ModelUsageObserved(
                    attempt_id=attempt_id,
                    actual_model=MODEL,
                    response_id=response_id,
                    usage=UsageObservation(
                        completeness="complete",
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    ),
                )
                yield ModelAttemptFinished(attempt_id=attempt_id, outcome="completed")
                yield event.model_copy(update={"usage": usage})
            else:
                yield event


def provider_context(provider):
    @asynccontextmanager
    async def context(_):
        yield provider

    return context


@pytest.mark.parametrize("task_version", [1, 2])
async def test_campaign_runs_two_isolated_trials_persists_report_and_reopens(
    tmp_path: Path,
    task_version: int,
) -> None:
    config = execution_config(tmp_path, task_version=task_version)
    provider = CostedHistoricalFixProvider()

    result = await run_coding_eval_campaign(
        config,
        allow_network=True,
        provider_factory=provider_context(provider),
    )

    root = Path(config.work_root)
    state = read_eval_campaign_execution_state(root / "campaign-state.json")
    report = read_eval_campaign_report(root / "campaign-report.json")
    assert result.reason == "completed" and result.report_published
    assert result.completed_trials == result.scheduled_trials == 2
    assert len(provider.requests) == 14
    assert state.status == "completed" and state.completed_run_ids == config.plan.run_ids
    assert report.summary.passed_trials == 2
    assert report.summary.model_attempts == 14
    assert report.summary.known_cost_amount == "0.00154"
    assert report.plan.task_version == task_version
    assert (
        report.plan.task_fingerprint
        == historical_coding_eval(TASK_ID, task_version).task.fingerprint
    )
    assert read_eval_campaign_plan(root / "campaign-plan.json") == config.plan
    assert mode(root) == 0o700
    for name in ("campaign-plan.json", "campaign-state.json", "campaign-report.json"):
        assert mode(root / name) == 0o600

    def forbidden(_):
        pytest.fail("已完成Campaign不得再次创建Provider")

    reopened = await run_coding_eval_campaign(
        config, allow_network=True, provider_factory=forbidden
    )
    assert reopened == result


async def test_plan_is_durable_before_provider_and_fee_limit_stops_next_trial(
    tmp_path: Path,
) -> None:
    config = execution_config(tmp_path, fee_stop_amount="0.000001")
    provider = CostedHistoricalFixProvider()

    def factory(checked):
        assert checked == config.provider_config
        assert read_eval_campaign_plan(Path(config.work_root) / "campaign-plan.json") == config.plan
        return provider_context(provider)(checked)

    result = await run_coding_eval_campaign(config, allow_network=True, provider_factory=factory)

    root = Path(config.work_root)
    state = read_eval_campaign_execution_state(root / "campaign-state.json")
    assert result.reason == "fee_limit_reached"
    assert result.completed_trials == 1 and not result.report_published
    assert len(provider.requests) == 7
    assert state.status == "stopped" and state.stop_reason == "fee_limit_reached"
    assert not (root / "campaign-report.json").exists()

    reopened = await run_coding_eval_campaign(
        config,
        allow_network=True,
        provider_factory=lambda _: pytest.fail("停止后不得创建Provider"),
    )
    assert reopened == result


async def test_unknown_cost_stops_and_crash_window_recovers_without_next_trial(
    tmp_path: Path,
) -> None:
    config = execution_config(tmp_path)
    provider = HistoricalFixProvider()
    crashed = False

    def fault(point: str) -> None:
        nonlocal crashed
        if point == "campaign.after_trial" and not crashed:
            crashed = True
            raise RuntimeError("simulated exit after completed trial")

    with pytest.raises(RuntimeError, match="simulated exit"):
        await run_coding_eval_campaign(
            config,
            allow_network=True,
            provider_factory=provider_context(provider),
            fault=fault,
        )
    assert len(provider.requests) == 7

    result = await run_coding_eval_campaign(
        config,
        allow_network=True,
        provider_factory=lambda _: pytest.fail("未知成本恢复不得创建Provider"),
    )
    state = read_eval_campaign_execution_state(Path(config.work_root) / "campaign-state.json")
    assert result.reason == "cost_unknown" and result.completed_trials == 1
    assert state.status == "stopped" and state.stop_reason == "cost_unknown"


async def test_report_publication_crash_recovers_without_provider(tmp_path: Path) -> None:
    config = execution_config(tmp_path)
    provider = CostedHistoricalFixProvider()

    def fault(point: str) -> None:
        if point == "campaign.after_report":
            raise RuntimeError("simulated exit after campaign report")

    with pytest.raises(RuntimeError, match="after campaign report"):
        await run_coding_eval_campaign(
            config,
            allow_network=True,
            provider_factory=provider_context(provider),
            fault=fault,
        )
    root = Path(config.work_root)
    state = read_eval_campaign_execution_state(root / "campaign-state.json")
    assert state.status == "running" and len(state.completed_run_ids) == 2
    assert exists(root / "campaign-report.json")

    recovered = await run_coding_eval_campaign(
        config,
        allow_network=True,
        provider_factory=lambda _: pytest.fail("报告恢复不得创建Provider"),
    )
    state = read_eval_campaign_execution_state(root / "campaign-state.json")
    assert recovered.reason == "completed" and state.status == "completed"


async def test_campaign_lock_and_source_revision_fail_before_provider(tmp_path: Path) -> None:
    config = execution_config(tmp_path)
    root = Path(config.work_root)
    create_private_roots(root)
    descriptor = os.open(root / ".campaign.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(KernelError) as busy:
            await run_coding_eval_campaign(
                config,
                allow_network=True,
                provider_factory=lambda _: pytest.fail("锁冲突不得创建Provider"),
            )
        assert busy.value.code == "eval_campaign_busy"
    finally:
        os.close(descriptor)

    body = config.model_dump(mode="json")
    body["plan"]["environment"]["harnessix_revision"] = "0" * 40
    body["work_root"] = str(tmp_path / "revision-campaign")
    changed = CodingEvalCampaignRunConfig.model_validate_json(json.dumps(body))
    with pytest.raises(KernelError) as revision_error:
        await run_coding_eval_campaign(
            changed,
            allow_network=True,
            provider_factory=lambda _: pytest.fail("revision漂移不得创建Provider"),
        )
    assert revision_error.value.code == "eval_campaign_source_revision_mismatch"


async def test_disabled_campaign_does_not_touch_files_or_provider(tmp_path: Path) -> None:
    config = execution_config(tmp_path)

    result = await run_coding_eval_campaign(
        config,
        provider_factory=lambda _: pytest.fail("禁网时不得创建Provider"),
    )

    assert result == CodingEvalCampaignRunReport(reason="network_not_enabled")
    assert not exists(Path(config.work_root))


def test_execution_config_rejects_retry_parallel_tools_and_source_child(tmp_path: Path) -> None:
    config = execution_config(tmp_path)
    body = config.model_dump(mode="json")
    body["provider_config"]["max_attempts"] = 2
    with pytest.raises(ValidationError):
        CodingEvalCampaignRunConfig.model_validate(body)
    body = config.model_dump(mode="json")
    body["provider_config"]["capabilities"]["parallel_tool_calls"] = True
    with pytest.raises(ValidationError):
        CodingEvalCampaignRunConfig.model_validate(body)
    body = config.model_dump(mode="json")
    body["work_root"] = str(ROOT / "private")
    with pytest.raises(ValidationError):
        CodingEvalCampaignRunConfig.model_validate(body)


def test_execution_state_is_private_atomic_and_rejects_tampering(tmp_path: Path) -> None:
    config = execution_config(tmp_path)
    root = Path(config.work_root)
    root.mkdir(mode=0o700)
    state = CodingEvalCampaignExecutionState(
        campaign_id=config.plan.campaign_id,
        plan_fingerprint=config.plan.fingerprint,
        execution_config_fingerprint=config.fingerprint,
        status="ready",
        completed_run_ids=(),
        known_cost_currency="CNY",
        known_cost_amount="0",
        started_at=utc_now(),
        updated_at=utc_now(),
    )
    from harnessix.evals.report import write_eval_campaign_execution_state

    path = root / "campaign-state.json"
    write_eval_campaign_execution_state(path, state)
    assert read_eval_campaign_execution_state(path) == state
    assert path.stat().st_mode & 0o777 == 0o600
    body = json.loads(path.read_text(encoding="utf-8"))
    body["status"] = "completed"
    path.write_text(json.dumps(body), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(KernelError) as error:
        read_eval_campaign_execution_state(path)
    assert error.value.code == "eval_campaign_execution_state_invalid"


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("coding-eval-campaign-run-config", CodingEvalCampaignRunConfig),
        ("coding-eval-campaign-execution-state", CodingEvalCampaignExecutionState),
        ("coding-eval-campaign-run-report", CodingEvalCampaignRunReport),
    ],
)
def test_campaign_execution_public_schema_is_frozen(name: str, model: type[ContractModel]) -> None:
    expected = json.loads(Path(f"spec/{name}-v1.schema.json").read_text(encoding="utf-8"))
    assert expected == model.model_json_schema()
