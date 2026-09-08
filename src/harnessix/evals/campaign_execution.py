"""默认禁网、可恢复且带试验间费用停止线的Coding Eval Campaign执行器。"""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Turn
from harnessix.agent.reducer import get_turn
from harnessix.domain.models import utc_now
from harnessix.evals.campaign import CompletedCodingEvalTrial, build_coding_eval_campaign_report
from harnessix.evals.campaign_execution_contracts import (
    CodingEvalCampaignExecutionState,
    CodingEvalCampaignRunConfig,
    CodingEvalCampaignRunReport,
)
from harnessix.evals.catalog import historical_coding_eval
from harnessix.evals.report import (
    eval_campaign_report_sha256,
    read_eval_campaign_execution_state,
    read_eval_campaign_plan,
    read_eval_campaign_report,
    read_eval_report,
    write_eval_campaign_execution_state,
    write_eval_campaign_plan,
    write_eval_campaign_report,
)
from harnessix.evals.run_state import read_eval_run_state
from harnessix.evals.runner import run_historical_coding_eval
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelProvider
from harnessix.models.costs import CostReportRecord, bind_price, build_cost_report
from harnessix.models.pricing import amount_units, format_amount
from harnessix.session.sqlite import SQLiteSessionStore

ProviderFactory = Callable[[OpenAIChatConfig], AbstractAsyncContextManager[ModelProvider]]
Fault = Callable[[str], None]

_PLAN_FILE = "campaign-plan.json"
_STATE_FILE = "campaign-state.json"
_REPORT_FILE = "campaign-report.json"
_RUNS_DIRECTORY = "runs"
_LOCK_FILE = ".campaign.lock"
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}


def _fault(_: str) -> None:
    return None


def _sdk_provider(config: OpenAIChatConfig) -> AbstractAsyncContextManager[ModelProvider]:
    from harnessix.models.openai_chat import OpenAIChatProvider

    return OpenAIChatProvider(config)


def _ensure_private_root(path: Path) -> None:
    try:
        if path.is_symlink():
            raise OSError
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o777 != 0o700:
            raise OSError
    except OSError:
        raise KernelError(
            "eval_campaign_work_root_invalid", "Campaign私有运行目录缺失、权限错误或不安全"
        ) from None


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


@contextmanager
def _campaign_lock(root: Path) -> Iterator[None]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            root / _LOCK_FILE,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 != 0o600:
            raise OSError
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise KernelError("eval_campaign_busy", "Campaign已有活跃执行宿主") from None
        yield
    except KernelError:
        raise
    except OSError:
        raise KernelError("eval_campaign_lock_invalid", "Campaign执行锁不可用") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _require_executable(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            raise OSError
        return path
    except OSError:
        raise KernelError(
            "eval_campaign_host_binding_invalid", f"Campaign{label}绑定无效"
        ) from None


def _source_revision(config: CodingEvalCampaignRunConfig) -> str:
    git = _require_executable(Path(config.git_executable), "Git程序")
    source = Path(config.source_root)
    try:
        result = subprocess.run(
            (
                str(git),
                "--no-pager",
                "--no-optional-locks",
                "-c",
                "core.hooksPath=/dev/null",
                "rev-parse",
                "HEAD",
            ),
            cwd=source,
            env=_GIT_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        revision = result.stdout.decode("ascii", errors="strict").strip()
        if result.returncode != 0 or revision != config.plan.environment.harnessix_revision:
            raise ValueError
        return revision
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise KernelError(
            "eval_campaign_source_revision_mismatch",
            "Campaign执行代码revision与计划环境不一致",
        ) from None


def _require_scope(config: CodingEvalCampaignRunConfig) -> None:
    definition = historical_coding_eval(config.plan.task_id, config.plan.task_version)
    task = definition.task
    if (
        task.task_version != config.plan.task_version
        or task.fingerprint != config.plan.task_fingerprint
        or config.plan.environment.platform != sys.platform
        or config.plan.environment.isolation != "private-managed-copy-no-os-sandbox"
    ):
        raise KernelError("eval_campaign_scope_mismatch", "Campaign任务或执行环境与计划不一致")
    _require_executable(Path(config.python_executable), "Python程序")
    _source_revision(config)


def _require_plan(path: Path, config: CodingEvalCampaignRunConfig) -> None:
    if path.exists() or path.is_symlink():
        if read_eval_campaign_plan(path) != config.plan:
            raise KernelError("eval_campaign_plan_mismatch", "已发布Campaign计划与配置不一致")
        return
    write_eval_campaign_plan(path, config.plan)


def _initial_state(config: CodingEvalCampaignRunConfig) -> CodingEvalCampaignExecutionState:
    now = utc_now()
    return CodingEvalCampaignExecutionState(
        campaign_id=config.plan.campaign_id,
        plan_fingerprint=config.plan.fingerprint,
        execution_config_fingerprint=config.fingerprint,
        status="ready",
        completed_run_ids=(),
        known_cost_currency=config.plan.price.currency,
        known_cost_amount="0",
        started_at=now,
        updated_at=now,
    )


def _require_state(
    state: CodingEvalCampaignExecutionState, config: CodingEvalCampaignRunConfig
) -> None:
    completed = state.completed_run_ids
    if (
        state.campaign_id != config.plan.campaign_id
        or state.plan_fingerprint != config.plan.fingerprint
        or state.execution_config_fingerprint != config.fingerprint
        or completed != config.plan.run_ids[: len(completed)]
        or state.known_cost_currency != config.plan.price.currency
    ):
        raise KernelError("eval_campaign_execution_mismatch", "Campaign执行状态与固定配置不一致")


async def _load_trial(
    config: CodingEvalCampaignRunConfig, run_id: UUID
) -> CompletedCodingEvalTrial:
    run_root = Path(config.work_root) / _RUNS_DIRECTORY / str(run_id)
    state = read_eval_run_state(run_root / "run-state.json")
    report = read_eval_report(run_root / "report.json")
    if state.thread_id is None or state.turn_id is None:
        raise KernelError("eval_campaign_evidence_invalid", "Campaign已完成运行缺少Thread或Turn")
    session = SQLiteSessionStore(run_root / "session.sqlite")
    turn: Turn = get_turn(await session.get_thread(state.thread_id), state.turn_id)
    try:
        bindings = tuple(
            bind_price(attempt, config.plan.price, config.plan.billing_context)
            for attempt in turn.accounted_attempts
        )
        cost = build_cost_report(turn, bindings)
    except ValueError:
        raise KernelError("eval_campaign_cost_invalid", "Campaign单次成本无法安全重算") from None
    return CompletedCodingEvalTrial(state=state, report=report, turn=turn, cost=cost)


def _known_cost(cost: CostReportRecord, currency: str) -> tuple[int, bool]:
    totals = cost.summary.totals
    if len(totals) > 1 or (totals and totals[0].currency != currency):
        raise KernelError("eval_campaign_cost_invalid", "Campaign单次成本币种不一致")
    units = amount_units(totals[0].known_amount) if totals else 0
    return units, cost.summary.completeness == "complete"


async def _rebuild_prefix(
    config: CodingEvalCampaignRunConfig,
    state: CodingEvalCampaignExecutionState,
) -> tuple[list[CompletedCodingEvalTrial], int, bool]:
    completed: list[CompletedCodingEvalTrial] = []
    total = 0
    all_complete = True
    for run_id in state.completed_run_ids:
        evidence = await _load_trial(config, run_id)
        units, complete = _known_cost(evidence.cost, config.plan.price.currency)
        completed.append(evidence)
        total += units
        all_complete = all_complete and complete
    if format_amount(total) != state.known_cost_amount:
        raise KernelError("eval_campaign_cost_mismatch", "Campaign已知成本与完成运行不一致")
    return completed, total, all_complete


def _run_report(
    config: CodingEvalCampaignRunConfig,
    state: CodingEvalCampaignExecutionState,
    reason: str,
    *,
    published: bool = False,
) -> CodingEvalCampaignRunReport:
    return CodingEvalCampaignRunReport.model_validate(
        {
            "reason": reason,
            "campaign_id": config.plan.campaign_id,
            "scheduled_trials": len(config.plan.run_ids),
            "completed_trials": len(state.completed_run_ids),
            "report_published": published,
            "known_cost_currency": state.known_cost_currency,
            "known_cost_amount": state.known_cost_amount,
        }
    )


async def _recover_published_report(
    config: CodingEvalCampaignRunConfig,
    state: CodingEvalCampaignExecutionState,
    report_path: Path,
) -> tuple[CodingEvalCampaignExecutionState, CodingEvalCampaignRunReport] | None:
    if not _path_present(report_path):
        return None
    completed, _, all_complete = await _rebuild_prefix(config, state)
    if len(completed) != len(config.plan.run_ids) or not all_complete:
        raise KernelError("eval_campaign_report_mismatch", "Campaign报告早于全部试验完成")
    expected = build_coding_eval_campaign_report(config.plan, tuple(completed))
    report = read_eval_campaign_report(report_path)
    if report != expected:
        raise KernelError("eval_campaign_report_mismatch", "Campaign报告与运行证据不一致")
    digest = eval_campaign_report_sha256(report)
    if state.status == "completed":
        if state.report_sha256 != digest:
            raise KernelError("eval_campaign_report_mismatch", "Campaign报告摘要不一致")
        return state, _run_report(config, state, "completed", published=True)
    state = state.model_copy(
        update={"status": "completed", "report_sha256": digest, "updated_at": utc_now()}
    )
    write_eval_campaign_execution_state(Path(config.work_root) / _STATE_FILE, state)
    return state, _run_report(config, state, "completed", published=True)


async def run_coding_eval_campaign(
    config: CodingEvalCampaignRunConfig,
    *,
    allow_network: bool = False,
    provider_factory: ProviderFactory | None = None,
    cancel: CancelToken | None = None,
    fault: Fault | None = None,
) -> CodingEvalCampaignRunReport:
    """顺序执行固定试验；费用未知或试验间达到停止线时不再创建下一请求。"""

    if allow_network is not True:
        return CodingEvalCampaignRunReport(reason="network_not_enabled")
    try:
        config = CodingEvalCampaignRunConfig.model_validate_json(
            config.model_dump_json(), strict=True
        )
    except ValueError:
        raise KernelError("eval_campaign_config_invalid", "Campaign执行配置无效") from None
    token = cancel or CancelToken()
    fail = fault or _fault
    root = Path(config.work_root)
    _ensure_private_root(root)
    _ensure_private_root(root / _RUNS_DIRECTORY)
    with _campaign_lock(root):
        plan_path = root / _PLAN_FILE
        state_path = root / _STATE_FILE
        report_path = root / _REPORT_FILE
        _require_plan(plan_path, config)
        fail("campaign.after_plan")
        if state_path.exists() or state_path.is_symlink():
            state = read_eval_campaign_execution_state(state_path)
        else:
            state = _initial_state(config)
            write_eval_campaign_execution_state(state_path, state)
        _require_state(state, config)
        recovered = await _recover_published_report(config, state, report_path)
        if recovered is not None:
            return recovered[1]
        completed, known_units, all_complete = await _rebuild_prefix(config, state)
        if state.status == "stopped":
            assert state.stop_reason is not None
            if (state.stop_reason == "cost_unknown") != (not all_complete):
                raise KernelError("eval_campaign_cost_mismatch", "Campaign停止原因与成本证据不一致")
            if state.stop_reason == "fee_limit_reached" and (
                known_units < amount_units(config.fee_stop_amount)
                or len(completed) == len(config.plan.run_ids)
            ):
                raise KernelError("eval_campaign_cost_mismatch", "Campaign费用停止证据无效")
            return _run_report(config, state, state.stop_reason)
        if state.status == "completed":
            raise KernelError("eval_campaign_report_mismatch", "Campaign完成状态缺少报告")
        if not all_complete:
            state = state.model_copy(
                update={
                    "status": "stopped",
                    "stop_reason": "cost_unknown",
                    "updated_at": utc_now(),
                }
            )
            write_eval_campaign_execution_state(state_path, state)
            return _run_report(config, state, "cost_unknown")
        fee_limit = amount_units(config.fee_stop_amount)
        if known_units >= fee_limit and len(completed) < len(config.plan.run_ids):
            state = state.model_copy(
                update={
                    "status": "stopped",
                    "stop_reason": "fee_limit_reached",
                    "updated_at": utc_now(),
                }
            )
            write_eval_campaign_execution_state(state_path, state)
            return _run_report(config, state, "fee_limit_reached")
        _require_scope(config)
        token.checkpoint()
        factory = provider_factory or _sdk_provider
        context = factory(config.provider_config)
        async with context as provider:
            for run_id in config.plan.run_ids[len(completed) :]:
                token.checkpoint()
                if known_units >= fee_limit:
                    state = state.model_copy(
                        update={
                            "status": "stopped",
                            "stop_reason": "fee_limit_reached",
                            "updated_at": utc_now(),
                        }
                    )
                    write_eval_campaign_execution_state(state_path, state)
                    return _run_report(config, state, "fee_limit_reached")
                state = state.model_copy(update={"status": "running", "updated_at": utc_now()})
                write_eval_campaign_execution_state(state_path, state)
                await run_historical_coding_eval(
                    Path(config.source_root),
                    root / _RUNS_DIRECTORY,
                    Path(config.git_executable),
                    Path(config.python_executable),
                    historical_coding_eval(config.plan.task_id, config.plan.task_version),
                    run_id,
                    provider,
                    config.plan.environment,
                    token,
                )
                evidence = await _load_trial(config, run_id)
                trial_units, cost_complete = _known_cost(evidence.cost, config.plan.price.currency)
                known_units += trial_units
                completed.append(evidence)
                state = state.model_copy(
                    update={
                        "completed_run_ids": tuple(config.plan.run_ids[: len(completed)]),
                        "known_cost_amount": format_amount(known_units),
                        "updated_at": utc_now(),
                    }
                )
                write_eval_campaign_execution_state(state_path, state)
                fail("campaign.after_trial")
                if not cost_complete:
                    state = state.model_copy(
                        update={
                            "status": "stopped",
                            "stop_reason": "cost_unknown",
                            "updated_at": utc_now(),
                        }
                    )
                    write_eval_campaign_execution_state(state_path, state)
                    return _run_report(config, state, "cost_unknown")
        report = build_coding_eval_campaign_report(config.plan, tuple(completed))
        write_eval_campaign_report(report_path, report)
        fail("campaign.after_report")
        state = state.model_copy(
            update={
                "status": "completed",
                "report_sha256": eval_campaign_report_sha256(report),
                "updated_at": utc_now(),
            }
        )
        write_eval_campaign_execution_state(state_path, state)
        return _run_report(config, state, "completed", published=True)
