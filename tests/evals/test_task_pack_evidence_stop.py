from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import harnessix.evals.task_pack_execution as case_execution
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ErrorContent,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    ThreadCreated,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
    UsageRecorded,
)
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.evals.grader import grade_coding_eval
from harnessix.evals.report import (
    eval_report_sha256,
    read_eval_campaign_execution_state,
    read_eval_campaign_plan,
    read_eval_suite_case_report,
    read_eval_suite_execution_state,
    read_eval_suite_plan,
    write_eval_campaign_execution_state,
    write_eval_report,
)
from harnessix.evals.run_state import write_eval_run_state
from harnessix.evals.suite_execution import run_coding_eval_suite
from harnessix.evals.task_pack_execution import TaskPackCaseExecutor
from harnessix.evals.task_pack_publication import open_task_pack_publication
from harnessix.evals.task_pack_suite import build_task_pack_suite_config
from harnessix.evals.task_pack_trial import TaskPackCodingEvalResult
from harnessix.models.pricing import amount_units, format_amount
from tests.evals.test_campaign import campaign_environment, evidence
from tests.evals.test_task_pack_execution import _case_and_campaign
from tests.models.pricing_helpers import NOW, context, price

_TRIAL_COST = "0.000025"


@pytest.fixture
def execution(tmp_path: Path):
    loaded, _, _ = _case_and_campaign()
    config = build_task_pack_suite_config(
        loaded,
        suite_id=uuid4(),
        work_root=tmp_path / "suite",
        environment=campaign_environment(),
        price=price(),
        billing_context=context(),
        fee_stop_amount="100",
        created_at=NOW - timedelta(seconds=1),
    )
    return loaded, config


def _executor(loaded, *, fault=None):
    def forbidden_provider(*_):
        pytest.fail("Trial端口测试不得打开真实Provider")

    kwargs = {} if fault is None else {"fault": fault}
    return TaskPackCaseExecutor(
        loaded, Path("/missing/git"), Path("/missing/docker"), forbidden_provider, **kwargs
    )


async def _write_fixture_trial(
    loaded, runs_root, case_id, run_id, environment, *, complete=True, publication_scope=None
):
    """仅在TempFS追加正式Session事件与失败评分证据，不运行模型或伪造通过结果。"""
    case = loaded.manifest.case(case_id)
    fixture = evidence(run_id, outcome="task" if complete else "provider", usage_complete=complete)
    attempt = fixture.turn.model_attempts[0]
    run_root = runs_root / str(run_id)
    run_root.mkdir(mode=0o700)
    async with open_task_pack_publication(run_root, publication_scope) as owner:
        sessions = owner.sessions
        thread_id, turn_id, user_id = uuid4(), uuid4(), uuid4()
        user = TextContent(kind="user_message", text=case.task.prompt)
        payloads = [
            TurnStarted(
                request_id=f"coding-eval:{run_id}",
                request_fingerprint="a" * 64,
                budget=case.task.budget,
            ),
            ItemStarted(item_id=user_id, content=user),
            ItemFinished(item_id=user_id, status=ItemStatus.COMPLETED, content=user),
            TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
            TurnStateChanged(status=TurnStatus.CALLING_MODEL),
            ModelAttemptStarted(
                attempt_id=attempt.attempt_id,
                step=1,
                index=1,
                provider=attempt.provider,
                requested_model=attempt.requested_model,
            ),
            ModelUsageObserved(
                attempt_id=attempt.attempt_id,
                usage=attempt.usage,
                actual_model=attempt.actual_model,
                response_id=attempt.response_id,
            ),
            ModelAttemptFinished(
                attempt_id=attempt.attempt_id, outcome=attempt.status, error=attempt.error
            ),
        ]
        if complete:
            payloads.extend(
                (
                    UsageRecorded(
                        step=1,
                        usage=Usage(
                            input_tokens=attempt.usage.input_tokens,
                            output_tokens=attempt.usage.output_tokens,
                        ),
                    ),
                    TurnStateChanged(status=TurnStatus.FINALIZING),
                    TurnStateChanged(status=TurnStatus.COMPLETED),
                )
            )
        else:
            error_id = uuid4()
            error = ErrorContent(failure=fixture.turn.error)
            payloads.extend(
                (
                    ItemStarted(item_id=error_id, content=error),
                    ItemFinished(item_id=error_id, status=ItemStatus.COMPLETED, content=error),
                    TurnStateChanged(status=TurnStatus.FAILED, error=fixture.turn.error),
                )
            )
        drafts = [
            EventDraft(
                payload=ThreadCreated(workspace=str(run_root / "workspace")), occurred_at=NOW
            ),
            *(
                EventDraft(turn_id=turn_id, payload=payload, occurred_at=NOW)
                for payload in payloads[:-1]
            ),
            EventDraft(
                turn_id=turn_id, payload=payloads[-1], occurred_at=NOW + timedelta(seconds=1)
            ),
        ]
        thread = await sessions.append(thread_id, drafts, expected_sequence=0)
        turn = thread.turns[0]
        report = grade_coding_eval(
            case.task,
            turn,
            run_id=run_id,
            environment=environment,
            started_at=turn.created_at,
            completed_at=turn.completed_at,
            baseline_observations=(),
            final_observations=(),
            git=fixture.report.git.model_copy(
                update={
                    "baseline_revision": case.task.repository.source_revision,
                    "baseline_tree_sha256": case.task.repository.baseline_tree_sha256,
                    "head_revision": case.task.repository.source_revision,
                    "changed_paths": (),
                }
            ),
        )
        assert report.outcome != "passed"
        state = fixture.state.model_copy(
            update={
                "task_id": case.task.task_id,
                "task_version": case.task.task_version,
                "task_fingerprint": case.task.fingerprint,
                "baseline_revision": case.task.repository.source_revision,
                "baseline_tree_sha256": case.task.repository.baseline_tree_sha256,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "baseline_observations": (),
                "environment": environment,
                "report_sha256": eval_report_sha256(report),
            }
        )
        write_eval_report(run_root / "report.json", report)
        write_eval_run_state(run_root / "run-state.json", state)
        return TaskPackCodingEvalResult(state, report, turn, run_root / "workspace")


def _trial_port(monkeypatch, *, completed_before_failure=0, error=None, complete_cost=True):
    """唯一替换点为Trial端口；Campaign、Case、Suite和证据恢复均执行原实现。"""
    calls = []
    failure = error if error is not None else KernelError("eval_baseline_invalid", "缺少可信终态")

    async def scripted_trial(
        loaded, runs_root, git, container, case_id, run_id, provider, environment, cancel, **kwargs
    ):
        calls.append((case_id, run_id))
        if len(calls) > completed_before_failure:
            raise failure
        return await _write_fixture_trial(
            loaded,
            runs_root,
            case_id,
            run_id,
            environment,
            complete=complete_cost,
            publication_scope=kwargs.get("publication_scope"),
        )

    monkeypatch.setattr(case_execution, "run_task_pack_coding_eval", scripted_trial)
    return calls, failure


def _json_snapshot(root: Path):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")}


def _assert_campaign_stop(root, campaign, reason, completed_trials):
    state = read_eval_campaign_execution_state(root / "campaign-state.json")
    assert read_eval_campaign_plan(root / "campaign-plan.json") == campaign
    assert state.status == "stopped" and state.stop_reason == reason
    assert state.completed_run_ids == campaign.run_ids[:completed_trials]
    assert state.known_cost_currency == campaign.price.currency
    assert state.report_sha256 is None
    assert not (root / "campaign-report.json").exists()
    assert not (root / "case-report.json").exists()
    assert not (root / "runs" / str(campaign.run_ids[completed_trials]) / "report.json").exists()
    assert {path.parent.name for path in (root / "runs").glob("*/report.json")} == {
        str(run_id) for run_id in campaign.run_ids[:completed_trials]
    }
    return state


@pytest.mark.parametrize("completed_trials", (0, 1))
async def test_case_evidence_stop_is_durable_without_trial_replay(
    tmp_path: Path, monkeypatch, execution, completed_trials
) -> None:
    loaded, config = execution
    expected, campaign = config.plan.cases[0], config.campaign_plans[0]
    calls, _ = _trial_port(monkeypatch, completed_before_failure=completed_trials)
    root = tmp_path / "case"

    result = await _executor(loaded)(expected, campaign, root, CancelToken())

    assert result.reason == "evidence_missing" and result.report is None
    state = _assert_campaign_stop(root, campaign, "evidence_missing", completed_trials)
    assert state.known_cost_amount == format_amount(completed_trials * amount_units(_TRIAL_COST))
    assert calls == [
        (expected.case_id, run_id) for run_id in campaign.run_ids[: completed_trials + 1]
    ]
    snapshot = _json_snapshot(root)
    # 仅将新合成夹具结算为静止主库，字节比较不承诺活跃 WAL/SHM 不变化。
    for database in (root / "runs").glob("*/session.sqlite"):
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        finally:
            connection.close()
    prefix_sha = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "runs").rglob("*")
        if path.is_file()
    }
    writes, providers, controls = [], [], []
    original_write = case_execution.write_eval_campaign_execution_state
    from harnessix.evals.task_pack_publication import TaskPackHistoryReadControl

    original_begin = TaskPackHistoryReadControl.begin

    def begin(cancel):
        control = original_begin(cancel)
        controls.append(control)
        return control

    def write(*args):
        writes.append(args)
        return original_write(*args)

    def no_provider(*args):
        providers.append(args)
        pytest.fail("持久停止恢复不得创建 Provider")

    monkeypatch.setattr(TaskPackHistoryReadControl, "begin", begin)
    monkeypatch.setattr(case_execution, "write_eval_campaign_execution_state", write)
    executor = TaskPackCaseExecutor(
        loaded, Path("/missing/git"), Path("/missing/docker"), no_provider
    )
    for cancelled in (False, True):
        token = CancelToken()
        if cancelled:
            token.cancel()
        if cancelled and completed_trials:
            # 非空完成前缀须沿原取消完整验真；不能绕过取消返回缓存停止结论。
            with pytest.raises(TurnCancelled):
                await executor(expected, campaign, root, token)
        else:
            assert await executor(expected, campaign, root, token) == result
        assert controls[-1].cancel is token
        assert providers == [] and writes == [] and len(calls) == completed_trials + 1
        assert {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (root / "runs").rglob("*")
            if path.is_file()
        } == prefix_sha
        assert read_eval_campaign_execution_state(root / "campaign-state.json") == state
        assert _json_snapshot(root) == snapshot
    assert len(calls) == completed_trials + 1


@pytest.mark.parametrize("completed_cases", (0, 1))
@pytest.mark.parametrize("completed_trials", (0, 1))
async def test_suite_persists_case_evidence_stop_and_resume_never_replays_trials(
    monkeypatch, execution, completed_cases, completed_trials
) -> None:
    loaded, config = execution
    calls, _ = _trial_port(
        monkeypatch, completed_before_failure=completed_cases * 2 + completed_trials
    )

    result = await run_coding_eval_suite(config, _executor(loaded))

    root = Path(config.work_root)
    expected = config.plan.cases[completed_cases]
    campaign = config.campaign_plans[completed_cases]
    case_root = root / "cases" / f"case-{completed_cases:03d}"
    case_state = _assert_campaign_stop(case_root, campaign, "evidence_missing", completed_trials)
    suite_state = read_eval_suite_execution_state(root / "suite-state.json")
    assert read_eval_suite_plan(root / "suite-plan.json") == config.plan
    assert result.reason == suite_state.stop_reason == "evidence_missing"
    assert suite_state.status == "stopped" and suite_state.report_sha256 is None
    assert result.completed_cases == completed_cases
    assert suite_state.completed_case_ids == tuple(
        case.case_id for case in config.plan.cases[:completed_cases]
    )
    assert result.current_case_id == suite_state.current_case_id == expected.case_id
    assert result.known_cost_currency == suite_state.known_cost_currency == campaign.price.currency
    assert (
        result.known_cost_amount
        == suite_state.known_cost_amount
        == format_amount(completed_cases * 2 * amount_units(_TRIAL_COST))
    )
    assert case_state.known_cost_amount == format_amount(
        completed_trials * amount_units(_TRIAL_COST)
    )
    assert not result.report_published and not (root / "suite-report.json").exists()
    for index in range(completed_cases):
        report = read_eval_suite_case_report(
            root / "cases" / f"case-{index:03d}" / "case-report.json"
        )
        assert report.case_id == config.plan.cases[index].case_id
    assert calls == [
        (case.case_id, run_id)
        for case, plan in zip(
            config.plan.cases[:completed_cases],
            config.campaign_plans[:completed_cases],
            strict=True,
        )
        for run_id in plan.run_ids
    ] + [(expected.case_id, run_id) for run_id in campaign.run_ids[: completed_trials + 1]]
    reports = {path: data for path, data in _json_snapshot(root).items() if "report" in path.name}
    for resume in (False, True, True, False):
        assert await run_coding_eval_suite(config, _executor(loaded), resume=resume) == result
        reopened = read_eval_suite_execution_state(root / "suite-state.json")
        assert reopened.status == "stopped" and reopened.stop_reason == "evidence_missing"
        assert reopened.completed_case_ids == suite_state.completed_case_ids
        assert reopened.known_cost_amount == suite_state.known_cost_amount
        assert read_eval_campaign_execution_state(case_root / "campaign-state.json") == case_state
        assert {
            path: data for path, data in _json_snapshot(root).items() if "report" in path.name
        } == reports
        assert not (root / "suite-report.json").exists()
    assert len(calls) == completed_cases * 2 + completed_trials + 1


@pytest.mark.parametrize("reason", ("evidence_missing", "cost_unknown", "fee_limit_reached"))
async def test_stopped_case_returns_original_reason_before_cancel_or_provider(
    tmp_path: Path, monkeypatch, execution, reason
) -> None:
    loaded, config = execution
    expected, campaign = config.plan.cases[0], config.campaign_plans[0]
    calls, _ = _trial_port(monkeypatch)
    root = tmp_path / "case"
    token = CancelToken()
    token.cancel()
    with pytest.raises(TurnCancelled):
        await _executor(loaded)(expected, campaign, root, token)
    path = root / "campaign-state.json"
    state = read_eval_campaign_execution_state(path).model_copy(
        update={"status": "stopped", "stop_reason": reason}
    )
    write_eval_campaign_execution_state(path, state)
    snapshot = _json_snapshot(root)

    result = await _executor(loaded)(expected, campaign, root, token)

    assert result.reason == reason and result.report is None
    assert read_eval_campaign_execution_state(path) == state
    assert _json_snapshot(root) == snapshot and not calls


async def test_unknown_trial_cost_remains_cost_unknown_through_case_suite_and_resume(
    monkeypatch, execution
) -> None:
    loaded, config = execution
    calls, _ = _trial_port(monkeypatch, completed_before_failure=1, complete_cost=False)

    result = await run_coding_eval_suite(config, _executor(loaded))

    root = Path(config.work_root)
    campaign = config.campaign_plans[0]
    case_root = root / "cases" / "case-000"
    state = _assert_campaign_stop(case_root, campaign, "cost_unknown", 1)
    assert result.reason == "cost_unknown" and result.completed_cases == 0
    assert state.known_cost_amount == result.known_cost_amount == "0"
    assert read_eval_suite_execution_state(root / "suite-state.json").stop_reason == "cost_unknown"
    for resume in (False, True):
        assert await run_coding_eval_suite(config, _executor(loaded), resume=resume) == result
        assert read_eval_campaign_execution_state(case_root / "campaign-state.json") == state
    assert calls == [(config.plan.cases[0].case_id, campaign.run_ids[0])]
    assert not (root / "suite-report.json").exists()


@pytest.mark.parametrize("before_trial", (False, True))
async def test_case_cancellation_remains_turn_cancelled_not_evidence_missing(
    tmp_path: Path, monkeypatch, execution, before_trial
) -> None:
    loaded, config = execution
    token = CancelToken()
    calls = []

    async def cancelled_trial(*args, **kwargs):
        calls.append(args[5])
        token.cancel()
        token.checkpoint()

    monkeypatch.setattr(case_execution, "run_task_pack_coding_eval", cancelled_trial)
    if before_trial:
        token.cancel()
    root = tmp_path / "case"

    with pytest.raises(TurnCancelled):
        await _executor(loaded)(config.plan.cases[0], config.campaign_plans[0], root, token)

    state = read_eval_campaign_execution_state(root / "campaign-state.json")
    assert state.status == ("ready" if before_trial else "running")
    assert state.stop_reason is None and state.completed_run_ids == ()
    assert state.known_cost_amount == "0" and state.report_sha256 is None
    assert len(calls) == (0 if before_trial else 1)
    assert not any("report" in path.name for path in _json_snapshot(root))


async def test_suite_cancellation_keeps_existing_cancelled_reason(monkeypatch, execution) -> None:
    loaded, config = execution
    token = CancelToken()

    async def cancelled_trial(*args, **kwargs):
        token.cancel()
        token.checkpoint()

    monkeypatch.setattr(case_execution, "run_task_pack_coding_eval", cancelled_trial)

    result = await run_coding_eval_suite(config, _executor(loaded), cancel=token)

    root = Path(config.work_root)
    assert result.reason == "cancelled" and result.completed_cases == 0
    state = read_eval_suite_execution_state(root / "suite-state.json")
    assert state.status == "stopped" and state.stop_reason == "cancelled"
    campaign_state = read_eval_campaign_execution_state(
        root / "cases" / "case-000" / "campaign-state.json"
    )
    assert campaign_state.status == "running" and campaign_state.stop_reason is None
    assert not any("report" in path.name for path in _json_snapshot(root))


@pytest.mark.parametrize("suite", (False, True), ids=("case", "suite"))
async def test_cancellation_preserves_completed_trial_prefix_and_known_cost(
    tmp_path: Path, monkeypatch, execution, suite
) -> None:
    loaded, config = execution
    cancelled = TurnCancelled()
    calls, _ = _trial_port(monkeypatch, completed_before_failure=1, error=cancelled)
    root = Path(config.work_root) if suite else tmp_path / "case"
    if suite:
        result = await run_coding_eval_suite(config, _executor(loaded))
        assert result.reason == "cancelled" and result.completed_cases == 0
        assert result.known_cost_amount == "0" and not result.report_published
        assert await run_coding_eval_suite(config, _executor(loaded)) == result
    else:
        with pytest.raises(TurnCancelled) as failure:
            await _executor(loaded)(
                config.plan.cases[0], config.campaign_plans[0], root, CancelToken()
            )
        assert failure.value is cancelled

    case_root = root / "cases" / "case-000" if suite else root
    state = read_eval_campaign_execution_state(case_root / "campaign-state.json")
    campaign = config.campaign_plans[0]
    assert state.status == "running" and state.stop_reason is None
    assert state.completed_run_ids == campaign.run_ids[:1]
    assert state.known_cost_amount == _TRIAL_COST and state.report_sha256 is None
    assert calls == [(config.plan.cases[0].case_id, run_id) for run_id in campaign.run_ids]
    assert {
        path.parent.name for path in _json_snapshot(case_root) if path.name == "report.json"
    } == {str(campaign.run_ids[0])}
    assert not (case_root / "campaign-report.json").exists()
    assert not (case_root / "case-report.json").exists()
    assert not (root / "suite-report.json").exists()


@pytest.mark.parametrize("suite", (False, True), ids=("case", "suite"))
@pytest.mark.parametrize(
    "error",
    (
        KernelError("storage_unavailable", "存储故障"),
        KernelError("eval_run_projection_invalid", "投影故障"),
        KernelError("eval_campaign_evidence_missing", "完成证据丢失"),
        KernelError("eval_campaign_cost_invalid", "成本故障"),
        KernelError("eval_baseline_invalid_suffix", "非白名单错误"),
        RuntimeError("host fault"),
    ),
    ids=("storage", "projection", "prefix-evidence", "cost", "similar-code", "runtime"),
)
async def test_non_whitelisted_trial_errors_propagate_without_publishing_or_stopping(
    tmp_path: Path, monkeypatch, execution, suite, error
) -> None:
    loaded, config = execution
    calls, _ = _trial_port(monkeypatch, error=error)
    root = Path(config.work_root) if suite else tmp_path / "case"

    with pytest.raises(type(error)) as failure:
        if suite:
            await run_coding_eval_suite(config, _executor(loaded))
        else:
            await _executor(loaded)(
                config.plan.cases[0], config.campaign_plans[0], root, CancelToken()
            )

    assert failure.value is error
    case_root = root / "cases" / "case-000" if suite else root
    state = read_eval_campaign_execution_state(case_root / "campaign-state.json")
    assert state.status == "running" and state.stop_reason is None
    assert state.completed_run_ids == () and state.known_cost_amount == "0"
    if suite:
        suite_state = read_eval_suite_execution_state(root / "suite-state.json")
        assert suite_state.status == "running" and suite_state.stop_reason is None
    assert calls == [(config.plan.cases[0].case_id, config.campaign_plans[0].run_ids[0])]
    assert not tuple(root.rglob("*report.json"))


@pytest.mark.parametrize("suite", (False, True), ids=("case", "suite"))
@pytest.mark.parametrize(
    "kernel_error", (False, True), ids=("runtime-fault", "baseline-code-fault")
)
async def test_after_trial_fault_propagates_and_preserves_committed_prefix(
    tmp_path: Path, monkeypatch, execution, suite, kernel_error
) -> None:
    loaded, config = execution
    calls, _ = _trial_port(monkeypatch, completed_before_failure=1)
    error = (
        KernelError("eval_baseline_invalid", "提交后故障")
        if kernel_error
        else RuntimeError("commit fault")
    )

    def fault(point):
        if point == "task_pack_case.after_trial":
            raise error

    executor = _executor(loaded, fault=fault)
    root = Path(config.work_root) if suite else tmp_path / "case"
    with pytest.raises(type(error)) as failure:
        if suite:
            await run_coding_eval_suite(config, executor)
        else:
            await executor(config.plan.cases[0], config.campaign_plans[0], root, CancelToken())

    assert failure.value is error
    case_root = root / "cases" / "case-000" if suite else root
    state = read_eval_campaign_execution_state(case_root / "campaign-state.json")
    assert state.status == "running" and state.stop_reason is None
    assert state.completed_run_ids == config.campaign_plans[0].run_ids[:1]
    assert state.known_cost_amount == _TRIAL_COST
    assert calls == [(config.plan.cases[0].case_id, config.campaign_plans[0].run_ids[0])]
    assert not (case_root / "campaign-report.json").exists()
    assert not (case_root / "case-report.json").exists()
    assert not (root / "suite-report.json").exists()
