"""离线导入Harbor冻结锁与原始结果；只选择结构化事实，不补造产品评分证据。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import RootModel

from harnessix.agent.errors import KernelError
from harnessix.evals.harbor_contracts import (
    HarborEvalReport,
    HarborEvalTrial,
    summarize_harbor_trials,
)
from harnessix.evals.report import _read_report


class _Source(RootModel[dict[str, Any]]):
    pass


def _read(path: Path) -> dict[str, Any]:
    return _read_report(
        path,
        _Source,
        8 * 1024 * 1024,
        invalid_code="harbor_source_invalid",
        label="Harbor源证据",
    ).root


def _digest(value: Any) -> str:
    body = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Harbor源字段必须为对象")
    return value


def _job_completion(value: str, source_timezone: str | None) -> str:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        if source_timezone is None:
            raise KernelError(
                "harbor_timezone_required", "Harbor运行时间缺少时区；需显式指定源时区"
            )
        zone = ZoneInfo(source_timezone)
        first, second = result.replace(tzinfo=zone, fold=0), result.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("Harbor时间存在夏令时歧义")
        result = first
    return result.isoformat()


def _outcome(
    result: dict[str, Any], native: dict[str, Any], reward: float | None, pass_reward: float
) -> dict[str, Any]:
    kind = _object(result.get("exception_info")).get("exception_type")
    reason = native.get("protocol_reason")
    agent_errors = {"NonZeroAgentExitCodeError", "AgentSafetyRefusalError", "AgentTimeoutError"}
    if kind and kind not in agent_errors:
        return {
            "outcome": "invalid",
            "classification": "eval_infrastructure",
            "reason": "execution_failed",
        }
    if reason == "deadline_exceeded" or kind == "AgentTimeoutError":
        return {"outcome": "failed", "classification": "budget", "reason": "deadline_exceeded"}
    if reason == "intervention_required":
        return {"outcome": "failed", "classification": "runtime", "reason": "intervention_required"}
    if native.get("native_failure_category") in {"provider", "budget"}:
        category = native["native_failure_category"]
        return {
            "outcome": "failed",
            "classification": category,
            "reason": "budget_exhausted" if category == "budget" else "execution_failed",
        }
    if kind:
        return {"outcome": "failed", "classification": "runtime", "reason": "execution_failed"}
    if reward is None:
        return {
            "outcome": "invalid",
            "classification": "eval_infrastructure",
            "reason": "verifier_missing",
        }
    info = _object(result.get("agent_info"))
    if not info.get("name") or (info.get("name") == "harnessix" and reason != "completed"):
        return {
            "outcome": "invalid",
            "classification": "eval_infrastructure",
            "reason": "execution_failed",
        }
    passed = reward == pass_reward
    return {
        "outcome": "passed" if passed else "failed",
        "classification": "passed" if passed else "task",
        "reason": "verified" if passed else "task_failed",
    }


def _trial(
    slot: int,
    plan: dict[str, Any],
    source: dict[str, Any] | None,
    job_id: UUID,
    reward_key: str,
    pass_reward: float,
) -> HarborEvalTrial:
    lock_digest = _digest(plan)
    common = {
        "slot": slot,
        "slot_id": str(uuid5(job_id, f"{slot}:{lock_digest}")),
        "task_name": _object(plan.get("task"))["name"],
        "task_digest": _object(plan.get("task"))["digest"],
        "input_lock_sha256": lock_digest,
        "agent_name": _object(plan.get("agent")).get("name") or "custom",
        "verifier_mode": "unknown",
    }
    if source is None:
        return HarborEvalTrial.model_validate_json(
            json.dumps(
                {
                    **common,
                    "outcome": "invalid",
                    "classification": "eval_infrastructure",
                    "reason": "missing_result",
                }
            )
        )
    agent = _object(source.get("agent_result"))
    native = _object(_object(agent.get("metadata")).get("harnessix"))
    if native and native.get("trial_id") != source["id"]:
        raise ValueError("Harbor原生协议摘要绑定了其他试次")
    reward = _object(_object(source.get("verifier_result")).get("rewards")).get(reward_key)
    if reward is not None and (type(reward) not in (int, float)):
        raise ValueError("Harbor奖励不是数值")
    exception = _object(source.get("exception_info"))
    info = _object(source.get("agent_info"))
    value = {
        **common,
        **_outcome(source, native, reward, pass_reward),
        "harbor_trial_id": source["id"],
        "source_result_sha256": _digest(source),
        "thread_id": native.get("thread_id"),
        "turn_id": native.get("turn_id"),
        "native_reason": native.get("protocol_reason"),
        "native_turn_status": native.get("turn_status"),
        "native_failure_category": native.get("native_failure_category"),
        "observed_model_steps": native.get("observed_model_steps"),
        "protocol_evidence_complete": native.get("protocol_evidence_complete"),
        "tool_results_succeeded": native.get("tool_results_succeeded"),
        "final_assistant_sha256": native.get("final_assistant_sha256"),
        "agent_name": info.get("name") or common["agent_name"],
        "agent_version": info.get("version"),
        "verifier_mode": source.get("verifier_environment_mode") or "unknown",
        "reward": reward,
        "exception_type": exception.get("exception_type"),
        "observed_input_tokens": native.get("observed_input_tokens", agent.get("n_input_tokens")),
        "observed_output_tokens": native.get(
            "observed_output_tokens", agent.get("n_output_tokens")
        ),
        "reported_cost_usd": agent.get("cost_usd"),
        "started_at": source.get("started_at"),
        "completed_at": source.get("finished_at"),
    }
    return HarborEvalTrial.model_validate_json(json.dumps(value, allow_nan=False))


def _validate_source_binding(
    child: Path, result: dict[str, Any], trial_lock: dict[str, Any], job_id: UUID
) -> None:
    config = _object(result.get("config"))
    if config.get("job_id") != str(job_id) or result["trial_name"] != child.name:
        raise ValueError("Harbor结果与运行目录身份不匹配")
    for field in ("agent", "environment"):
        actual = _object(config.get(field))
        if any(
            _digest(actual.get(key)) != _digest(value)
            for key, value in _object(trial_lock.get(field)).items()
        ):
            raise ValueError("Harbor结果执行配置与冻结锁不一致")
    if result["task_name"] != _object(trial_lock.get("task"))["name"]:
        raise ValueError("Harbor任务与冻结锁不匹配")


def _collect_trials(
    directory: Path, plans: list[dict[str, Any]], job_id: UUID
) -> dict[int, HarborEvalTrial]:
    available: dict[str, deque[int]] = defaultdict(deque)
    for slot, plan in enumerate(plans):
        available[_digest(_object(plan))].append(slot)
    collected: dict[int, HarborEvalTrial] = {}
    identities: set[str] = set()
    children = list(directory.iterdir())
    if len(children) > 1100:
        raise ValueError("Harbor运行目录超出边界")
    for child in sorted(children):
        if not child.is_dir() or not (child / "result.json").exists():
            continue
        if child.is_symlink():
            raise ValueError("Harbor试次目录不能是链接")
        trial_lock = _read(child / "lock.json")
        slots = available[_digest(trial_lock)]
        if not slots:
            raise ValueError("Harbor结果不在冻结输入锁中或出现重复结果")
        result = _read(child / "result.json")
        _validate_source_binding(child, result, trial_lock, job_id)
        if result["id"] in identities:
            raise ValueError("Harbor任务或试次身份不匹配")
        identities.add(result["id"])
        slot = slots.popleft()
        collected[slot] = _trial(slot, plans[slot], result, job_id, "reward", 1.0)
    return collected


def _require_zero_retries(lock: dict[str, Any], job: dict[str, Any]) -> None:
    retry_budget = _object(lock.get("retry")).get("max_retries")
    retries = _object(job.get("stats")).get("n_retries")
    if (
        type(retry_budget) is not int
        or retry_budget != 0
        or type(retries) is not int
        or retries != 0
    ):
        raise KernelError("harbor_retries_unsupported", "v1只接受明确零自动重试的Harbor运行")


def import_harbor_job(directory: Path, *, source_timezone: str | None = None) -> HarborEvalReport:
    """按零自动重试的冻结锁逐槽导入；保留缺失结果，拒绝重复和身份冲突。

    v1固定采用reward==1，不读取日志或异常正文，不联网、不重跑；原始大结果逐个处理。
    """
    lock, job = _read(directory / "lock.json"), _read(directory / "result.json")
    if not job.get("finished_at"):
        raise KernelError("harbor_job_incomplete", "Harbor运行尚未结束，不能发布最终报告")
    if lock.get("schema_version") != 3 or _object(lock.get("harbor")).get("version") != "0.25.0":
        raise ValueError("Harbor输入锁版本不受支持")
    _require_zero_retries(lock, job)
    plans = lock["trials"]
    if (
        not isinstance(plans, list)
        or not 1 <= len(plans) <= 1000
        or job["n_total_trials"] != len(plans)
    ):
        raise ValueError("Harbor冻结槽位与计划总数不一致")
    job_id = UUID(job["id"])
    collected = _collect_trials(directory, plans, job_id)
    trials = tuple(
        collected[slot] if slot in collected else _trial(slot, plan, None, job_id, "reward", 1.0)
        for slot, plan in enumerate(plans)
    )
    value = {
        "job_id": str(job_id),
        "source_lock_sha256": _digest(lock),
        "source_job_result_sha256": _digest(job),
        "plan_created_at": lock["created_at"],
        "completed_at": _job_completion(job["finished_at"], source_timezone),
        "source_timezone": source_timezone,
        "trials": [trial.model_dump(mode="json") for trial in trials],
        "summary": summarize_harbor_trials(trials).model_dump(mode="json"),
    }
    return HarborEvalReport.model_validate_json(json.dumps(value, allow_nan=False))


def safe_import_harbor_job(
    directory: Path,
    *,
    source_timezone: str | None = None,
) -> HarborEvalReport:
    """将外部结构错误收敛为固定分类，禁止将异常正文写入CLI或遥测。"""

    try:
        return import_harbor_job(
            directory,
            source_timezone=source_timezone,
        )
    except (KeyError, TypeError, ValueError, OSError, RecursionError):
        raise KernelError(
            "harbor_source_invalid", "Harbor源证据缺失、身份不一致或格式无效"
        ) from None
