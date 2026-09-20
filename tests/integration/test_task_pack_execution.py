from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.ids import new_id
from harnessix.agent.models import Usage
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.domain.models import utc_now
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.contracts import CodingEvalEnvironment
from harnessix.evals.report import read_eval_report
from harnessix.evals.suite_contracts import CodingEvalSuiteCasePlan
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_execution import TaskPackCaseExecutor
from harnessix.evals.task_pack_materializer import materialize_task_pack_case
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.pricing import BillingContext, FlatInputPrice, PriceSnapshot

_ROOT = Path(__file__).resolve().parents[2]
_SOLUTIONS = _ROOT / "benchmarks/taskpacks/harnessix-engineering-v1/solutions"
_MODEL = "harnessix-recorded-v1"
_CASE_ID = "agents-normalize-tool-name"


def _executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        pytest.skip(f"{name}不可用")
    return Path(value).resolve()


def _revision(git: Path) -> str:
    return subprocess.check_output(
        (str(git), "rev-parse", "HEAD"),
        cwd=_ROOT,
        text=True,
    ).strip()


def _fixed_plan(case, revision: str) -> tuple[CodingEvalSuiteCasePlan, CodingEvalCampaignPlan]:
    now = utc_now()
    price = PriceSnapshot(
        version="task-pack-recorded-v1",
        source_url="https://harnessix.invalid/pricing/recorded-v1",
        billing_provider="harnessix-recorded",
        model=_MODEL,
        region="offline",
        service_tier="eval",
        inference_mode="recorded",
        currency="USD",
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        input_tokens_min=0,
        input_tokens_max=None,
        input_price=FlatInputPrice(per_million="0"),
        output_per_million="0",
    )
    billing = BillingContext(
        billing_provider="harnessix-recorded",
        region="offline",
        service_tier="eval",
        inference_mode="recorded",
    )
    environment = CodingEvalEnvironment(
        harnessix_revision=revision,
        provider="recorded",
        model=_MODEL,
        platform=sys.platform,
        isolation="fixed-container-no-network",
    )
    campaign = CodingEvalCampaignPlan(
        campaign_id=uuid4(),
        task_id=case.task.task_id,
        task_version=case.task.task_version,
        task_fingerprint=case.task.fingerprint,
        environment=environment,
        run_ids=(uuid4(), uuid4()),
        price=price,
        billing_context=billing,
        created_at=now,
    )
    expected = CodingEvalSuiteCasePlan(
        case_id=case.case_id,
        task_kind=case.task_kind,
        task_id=case.task.task_id,
        task_version=case.task.task_version,
        task_fingerprint=case.task.fingerprint,
        repository=case.task.repository,
        campaign_plan_fingerprint=campaign.fingerprint,
    )
    return expected, campaign


def _solution(tmp_path: Path, git: Path):
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 1)
    case = loaded.manifest.case(_CASE_ID)
    root = tmp_path / "oracle"
    root.mkdir(mode=0o700)
    materialized = materialize_task_pack_case(loaded, root, git, case.case_id, uuid4())
    target = materialized.workspace / case.task.allowed_changed_paths[0]
    before = target.read_bytes() if target.exists() else None
    result = subprocess.run(
        (
            str(git),
            "apply",
            "--unidiff-zero",
            "--whitespace=nowarn",
            str(_SOLUTIONS / f"{case.case_id}.patch"),
        ),
        cwd=materialized.workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0
    return loaded, case, before, target.read_text(encoding="utf-8"), target.stat().st_mode & 0o777


class _RecordedSolutionProvider:
    """测试侧Recorded Provider；Golden不进入Adapter、Wheel或报告。"""

    def __init__(
        self,
        case,
        before: bytes | None,
        content: str,
        mode: int,
        run_id: UUID,
    ) -> None:
        self._case = case
        self._before = before
        self._content = content
        self._mode = mode
        self._run_id = run_id
        self.requests: list[ModelRequest] = []

    def _patch(self) -> dict[str, object]:
        path = self._case.task.allowed_changed_paths[0]
        if self._before is None:
            file = {
                "operation": "create",
                "path": path,
                "content": self._content,
                "mode": self._mode,
            }
        else:
            file = {
                "operation": "replace",
                "path": path,
                "expected_sha256": hashlib.sha256(self._before).hexdigest(),
                "content": self._content,
                "mode": self._mode,
            }
        return {"files": [file]}

    async def stream(
        self,
        request: ModelRequest,
        cancel: CancelToken,
    ) -> AsyncGenerator[ProviderEvent, None]:
        self.requests.append(request.model_copy(deep=True))
        cancel.checkpoint()
        attempt_id = new_id()
        response_id = f"recorded-{self._run_id}-{request.step}"
        usage = Usage(input_tokens=10, output_tokens=5)
        yield ModelAttemptStarted(
            attempt_id=attempt_id,
            step=request.step,
            index=1,
            provider="recorded",
            requested_model=_MODEL,
        )
        yield ResponseStarted(response_id=response_id)
        profile = self._case.profile_id
        if request.step == 1:
            event: ProviderEvent = ToolCallCompleted(
                call_id=f"{response_id}-profile-baseline",
                tool=f"run_profile.{profile}",
                arguments={"profile": profile, "selectors": []},
            )
            finish = "tool_calls"
        elif request.step == 2:
            event = ToolCallCompleted(
                call_id=f"{response_id}-patch",
                tool="apply_patch_batch",
                arguments=self._patch(),
            )
            finish = "tool_calls"
        elif request.step == 3:
            event = ToolCallCompleted(
                call_id=f"{response_id}-profile-final",
                tool=f"run_profile.{profile}",
                arguments={"profile": profile, "selectors": []},
            )
            finish = "tool_calls"
        elif request.step == 4:
            event = ToolCallCompleted(
                call_id=f"{response_id}-status",
                tool="git_status",
                arguments={},
            )
            finish = "tool_calls"
        elif request.step == 5:
            event = ToolCallCompleted(
                call_id=f"{response_id}-diff",
                tool="git_diff",
                arguments={"target": "worktree", "context_lines": 1},
            )
            finish = "tool_calls"
        else:
            assert request.step == 6
            answer = json.dumps(
                {
                    "summary": "Recorded Provider完成固定离线任务",
                    "changed_paths": list(self._case.task.allowed_changed_paths),
                    "tests": [{"profile": profile, "passed": True}],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield TextStarted(content_id=f"{response_id}-answer")
            yield TextCompleted(content_id=f"{response_id}-answer", text=answer)
            event = None
            finish = "completed"
        if event is not None:
            yield event
        yield ModelUsageObserved(
            attempt_id=attempt_id,
            actual_model=_MODEL,
            response_id=response_id,
            usage=UsageObservation(
                completeness="complete",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            ),
        )
        yield ModelAttemptFinished(attempt_id=attempt_id, outcome="completed")
        yield ResponseCompleted(finish_reason=finish, usage=usage)


@pytest.mark.skipif(os.name != "posix", reason="Task Pack Container Adapter要求POSIX宿主")
async def test_task_pack_case_runs_two_trials_through_formal_agent_campaign_and_reopens(
    tmp_path: Path,
) -> None:
    docker = _executable("docker")
    git = _executable("git")
    loaded, case, before, content, mode = _solution(tmp_path, git)
    source_profile = loaded.manifest.profile(case.profile_id)
    image_environment = (
        "HARNESSIX_TEST_PYTHON_IMAGE"
        if source_profile.language == "python"
        else "HARNESSIX_TEST_NODE_IMAGE"
    )
    if os.environ.get(image_environment) != source_profile.image:
        pytest.skip("未配置Task Pack固定镜像")
    expected, campaign = _fixed_plan(case, _revision(git))
    providers: list[_RecordedSolutionProvider] = []
    opened_run_ids: set[UUID] = set()

    def provider_factory(selected, run_id):
        assert selected == case
        assert run_id not in opened_run_ids, "完成Turn恢复不得重新打开Provider"
        opened_run_ids.add(run_id)

        @asynccontextmanager
        async def context():
            provider = _RecordedSolutionProvider(case, before, content, mode, run_id)
            providers.append(provider)
            yield provider

        return context()

    case_root = tmp_path / "case"
    case_root.mkdir(mode=0o700)
    report_crashed = False
    campaign_crashed = False

    def fault(point: str) -> None:
        nonlocal report_crashed, campaign_crashed
        if point == "task_pack_runner.after_report" and not report_crashed:
            report_crashed = True
            raise RuntimeError("模拟Trial报告写入后宿主崩溃")
        if point == "task_pack_case.after_campaign_report" and not campaign_crashed:
            campaign_crashed = True
            raise RuntimeError("模拟Campaign报告写入后宿主崩溃")

    executor = TaskPackCaseExecutor(loaded, git, docker, provider_factory, fault=fault)

    with pytest.raises(RuntimeError, match="Trial报告写入后"):
        await executor(expected, campaign, case_root, CancelToken())

    with pytest.raises(RuntimeError, match="Campaign报告写入后"):
        await executor(expected, campaign, case_root, CancelToken())

    def forbidden(*_args):
        raise AssertionError("完整Case恢复不得创建Provider")

    recovered = TaskPackCaseExecutor(loaded, git, docker, forbidden)
    result = await recovered(expected, campaign, case_root, CancelToken())

    assert result.reason == "completed" and result.report is not None
    failed_checks = {
        str(run_id): tuple(
            check.code
            for check in read_eval_report(case_root / "runs" / str(run_id) / "report.json").checks
            if not check.passed
        )
        for run_id in campaign.run_ids
    }
    assert result.report.campaign.summary.passed_trials == 2, failed_checks
    assert result.report.campaign.summary.cost_completeness == "complete"
    assert result.report.campaign.summary.known_cost_amount == "0"
    assert opened_run_ids == set(campaign.run_ids)
    assert len(providers) == 2 and [len(provider.requests) for provider in providers] == [6, 6]
    assert all(item.automated_approval_decisions == 3 for item in result.report.transcripts)
    assert all(item.human_intervention_count == 0 for item in result.report.transcripts)
    assert all(item.outcome == "passed" for item in result.report.tests)
    for run_id in campaign.run_ids:
        root = case_root / "runs" / str(run_id)
        assert (root / "session.sqlite").is_file()
        assert (root / "action-audit.db").is_file()
        assert (root / "process-owner/process-leases.db").is_file()
        assert (root / "workspace-transactions/transactions.db").is_file()
        assert (root / "run-state.json").stat().st_mode & 0o777 == 0o600
        assert (root / "report.json").stat().st_mode & 0o777 == 0o600

    assert await recovered(expected, campaign, case_root, CancelToken()) == result
