"""Harbor执行方式：把零自动重试的Job目录导入为一个组别的全部槽位，并提供奖励判分器。

导入只读冻结输入锁和原始结果，不读日志或异常正文，不联网、不重跑；缺失结果保留为missing。
智能体专属字段由按智能体名称选择的解析器给出，只进入system_refs。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from evalkit.contracts import (
    ArmSpec,
    ArtifactRef,
    GraderSpec,
    RunPlan,
    Score,
    TrialRecord,
    TrialStatus,
    Usage,
)
from evalkit.errors import EvalkitError
from evalkit.files import artifact_ref, read_artifact, read_json_object

HARBOR_VERSION = "0.25.0"
RESULT_ARTIFACT = "harbor_result"
# Harbor验收写在verifier/下的小文件；按存在登记，判分器可按名读取。
VERIFIER_ARTIFACTS = {
    "reward.txt": "harbor_verifier_reward_txt",
    "reward.json": "harbor_verifier_reward_json",
    "report.json": "harbor_verifier_report",
}
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_AGENT_ARTIFACT_BYTES = 32 * 1024 * 1024
_EXCEPTION_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")
_AGENT_EXCEPTIONS = frozenset(
    {"NonZeroAgentExitCodeError", "AgentSafetyRefusalError", "AgentTimeoutError"}
)


@dataclass(frozen=True)
class AgentResult:
    """智能体专属解析结果。status为None表示智能体自身报告正常结束，由通用规则决定。"""

    status: TrialStatus | None = None
    reason: str | None = None
    usage: Usage | None = None
    artifact_files: Mapping[str, Path] = field(default_factory=dict)
    system_refs: Mapping[str, str] = field(default_factory=dict)


class HarborResultParser(Protocol):
    """按Harbor结果中的智能体名称选择。unique_refs中的引用在同一Job内必须唯一。"""

    agent_name: str
    unique_refs: tuple[str, ...]

    def parse(self, trial_dir: Path, result: Mapping[str, Any]) -> AgentResult: ...


# ---------------------------------------------------------------- 源证据读取


def _object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Harbor源字段必须为对象")
    return value


def _digest(value: Any) -> str:
    body = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _time(value: Any, source_timezone: str | None) -> datetime | None:
    if value is None:
        return None
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        if source_timezone is None:
            raise EvalkitError("harbor_timezone_required", "Harbor时间缺少时区；需显式指定源时区")
        zone = ZoneInfo(source_timezone)
        first, second = result.replace(tzinfo=zone, fold=0), result.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("Harbor时间存在夏令时歧义")
        result = first
    return result


def _require_supported_job(lock: dict[str, Any], job: dict[str, Any]) -> None:
    if not job.get("finished_at"):
        raise EvalkitError("harbor_job_incomplete", "Harbor运行尚未结束，不能导入")
    if lock.get("schema_version") != 3 or _object(lock.get("harbor")).get("version") != (
        HARBOR_VERSION
    ):
        raise ValueError("Harbor输入锁版本不受支持")
    budget = _object(lock.get("retry")).get("max_retries")
    retries = _object(job.get("stats")).get("n_retries")
    if type(budget) is not int or budget != 0 or type(retries) is not int or retries != 0:
        raise EvalkitError("harbor_retries_unsupported", "只接受明确零自动重试的Harbor运行")


# ---------------------------------------------------------------- 冻结锁与计划槽位


def _harbor_arm(plan: RunPlan, arm_id: str) -> ArmSpec:
    arm = next((arm for arm in plan.arms if arm.arm_id == arm_id), None)
    if arm is None or arm.executor != "harbor":
        raise EvalkitError("harbor_arm_invalid", "组别不在计划内或执行方式不是Harbor")
    return arm


def _bind_slots(trial_locks: list[Any], plan: RunPlan, arm_id: str) -> list[int]:
    """冻结锁中的每个试次对应计划的一个槽位；同一任务的第k次出现即第k次重复。"""
    items = {item.item_id: item for item in plan.dataset.items}
    slots = {key: index for index, key in enumerate(plan.slots())}
    attempts: dict[str, int] = defaultdict(int)
    bound = []
    for trial_lock in trial_locks:
        task = _object(_object(trial_lock).get("task"))
        name = task["name"]
        item = items.get(name)
        if item is None or item.item_digest != task["digest"]:
            raise ValueError("Harbor任务不在计划内或任务摘要与计划不一致")
        attempt = attempts[name]
        attempts[name] += 1
        if attempt >= plan.attempts_per_item:
            raise ValueError("Harbor重复次数超出计划")
        bound.append(slots[(arm_id, name, attempt)])
    if any(attempts[item_id] != plan.attempts_per_item for item_id in items):
        raise ValueError("Harbor Job未覆盖计划的全部条目和重复")
    return bound


def _collect_results(
    job_dir: Path, trial_locks: list[Any], job_id: UUID
) -> dict[int, tuple[Path, dict[str, Any]]]:
    """按试次锁摘要把结果目录分配给冻结槽位；多余、重复或身份不符的结果直接拒绝。"""
    available: dict[str, deque[int]] = defaultdict(deque)
    for index, trial_lock in enumerate(trial_locks):
        available[_digest(_object(trial_lock))].append(index)
    children = list(job_dir.iterdir())
    if len(children) > 11_000:
        raise ValueError("Harbor运行目录超出边界")
    collected: dict[int, tuple[Path, dict[str, Any]]] = {}
    identities: set[str] = set()
    for child in sorted(children):
        if not child.is_dir() or not (child / "result.json").exists():
            continue
        if child.is_symlink():
            raise ValueError("Harbor试次目录不能是链接")
        trial_lock = read_json_object(child / "lock.json", _MAX_SOURCE_BYTES)
        indexes = available[_digest(trial_lock)]
        if not indexes:
            raise ValueError("Harbor结果不在冻结输入锁中或出现重复结果")
        result = read_json_object(child / "result.json", _MAX_SOURCE_BYTES)
        _validate_binding(child, result, trial_lock, job_id)
        if result["id"] in identities:
            raise ValueError("Harbor试次身份重复")
        identities.add(result["id"])
        collected[indexes.popleft()] = (child, result)
    return collected


def _validate_binding(
    child: Path, result: dict[str, Any], trial_lock: dict[str, Any], job_id: UUID
) -> None:
    config = _object(result.get("config"))
    if config.get("job_id") != str(job_id) or result["trial_name"] != child.name:
        raise ValueError("Harbor结果与运行目录身份不匹配")
    for section in ("agent", "environment"):
        actual = _object(config.get(section))
        if any(
            _digest(actual.get(key)) != _digest(value)
            for key, value in _object(trial_lock.get(section)).items()
        ):
            raise ValueError("Harbor结果执行配置与冻结锁不一致")
    if result["task_name"] != _object(trial_lock.get("task"))["name"]:
        raise ValueError("Harbor任务与冻结锁不匹配")
    rewards = _object(_object(result.get("verifier_result")).get("rewards"))
    if any(type(value) not in (int, float) for value in rewards.values()):
        raise ValueError("Harbor奖励不是数值")


# ---------------------------------------------------------------- 试次记录


def _status(
    exception_type: str | None, agent_name: str | None, parsed: AgentResult
) -> tuple[TrialStatus, str]:
    """基础设施异常优先；其次超时、智能体自报失败、智能体异常退出，最后才是正常完成。"""
    if exception_type is not None and exception_type not in _AGENT_EXCEPTIONS:
        return "infra_error", "harbor_infrastructure_error"
    if exception_type == "AgentTimeoutError":
        return "timeout", "deadline_exceeded"
    reported = parsed.status
    if reported is not None and reported != "completed":
        return reported, parsed.reason or reported
    if exception_type is not None:
        return "system_error", "agent_failed"
    if agent_name is None:
        return "infra_error", "agent_unidentified"
    return "completed", "completed"


def _harbor_usage(agent: dict[str, Any]) -> Usage:
    tokens = (agent.get("n_input_tokens"), agent.get("n_output_tokens"))
    if all(value is None for value in tokens):
        return Usage()
    return Usage(completeness="partial", input_tokens=tokens[0], output_tokens=tokens[1])


@dataclass(frozen=True)
class _JobContext:
    plan: RunPlan
    arm: ArmSpec
    job_id: UUID
    created_at: datetime
    finished_at: datetime
    parsers: Mapping[str, HarborResultParser]
    evidence_root: Path
    source_timezone: str | None


def _missing(slot: int, trial_lock: Any, context: _JobContext) -> TrialRecord:
    _, item_id, attempt = context.plan.slots()[slot]
    return TrialRecord(
        slot=slot,
        arm_id=context.arm.arm_id,
        item_id=item_id,
        attempt=attempt,
        status="missing",
        reason="missing_result",
        executor_refs={
            "harbor_job_id": str(context.job_id),
            "input_lock_sha256": _digest(trial_lock),
        },
    )


def _record(
    slot: int, trial_lock: Any, trial_dir: Path, result: dict[str, Any], context: _JobContext
) -> TrialRecord:
    _, item_id, attempt = context.plan.slots()[slot]
    info = _object(result.get("agent_info"))
    agent_name = info.get("name")
    if agent_name is not None and agent_name != context.arm.system:
        raise ValueError("Harbor智能体与组别系统不一致")
    exception_type = _object(result.get("exception_info")).get("exception_type")
    if exception_type is not None and not (
        isinstance(exception_type, str) and _EXCEPTION_TYPE.fullmatch(exception_type)
    ):
        raise ValueError("Harbor异常类型格式无效")
    parser = context.parsers.get(agent_name) if agent_name else None
    parsed = parser.parse(trial_dir, result) if parser else AgentResult()
    status, reason = _status(exception_type, agent_name, parsed)
    agent = _object(result.get("agent_result"))
    started = _time(result.get("started_at"), context.source_timezone)
    completed = _time(result.get("finished_at"), context.source_timezone)
    if (started and started < context.created_at) or (
        completed and completed > context.finished_at
    ):
        raise ValueError("Harbor试次时间超出Job窗口")
    executor_refs = {
        "harbor_job_id": str(context.job_id),
        "harbor_trial_id": str(UUID(result["id"])),
        "input_lock_sha256": _digest(trial_lock),
        "agent_name": agent_name,
        "agent_version": info.get("version"),
        "verifier_mode": result.get("verifier_environment_mode"),
        "exception_type": exception_type,
        "reported_cost_usd": None if agent.get("cost_usd") is None else str(agent["cost_usd"]),
    }
    artifacts = [
        artifact_ref(
            RESULT_ARTIFACT, trial_dir / "result.json", context.evidence_root, _MAX_SOURCE_BYTES
        )
    ]
    for filename, name in VERIFIER_ARTIFACTS.items():
        path = trial_dir / "verifier" / filename
        if path.is_file() and not path.is_symlink():
            artifacts.append(artifact_ref(name, path, context.evidence_root, _MAX_SOURCE_BYTES))
    for name, path in sorted(parsed.artifact_files.items()):
        if not path.resolve(strict=True).is_relative_to(trial_dir.resolve()):
            raise ValueError("智能体制品不在试次目录内")
        artifacts.append(artifact_ref(name, path, context.evidence_root, _MAX_AGENT_ARTIFACT_BYTES))
    return TrialRecord(
        slot=slot,
        arm_id=context.arm.arm_id,
        item_id=item_id,
        attempt=attempt,
        status=status,
        reason=reason,
        usage=parsed.usage if parsed.usage is not None else _harbor_usage(agent),
        started_at=started,
        completed_at=completed,
        artifacts=tuple(artifacts),
        executor_refs={key: str(value) for key, value in executor_refs.items() if value},
        system_refs=dict(parsed.system_refs),
    )


def _require_unique_refs(
    trials: list[TrialRecord], parsers: Mapping[str, HarborResultParser]
) -> None:
    for parser in parsers.values():
        for key in parser.unique_refs:
            values = [t.system_refs[key] for t in trials if key in t.system_refs]
            if len(values) != len(set(values)):
                raise ValueError("同一系统引用不能重复计入独立试次")


def import_harbor_job(
    job_dir: Path,
    plan: RunPlan,
    arm_id: str,
    parsers: Mapping[str, HarborResultParser],
    evidence_root: Path,
    *,
    source_timezone: str | None = None,
) -> tuple[TrialRecord, ...]:
    """把一个Harbor Job导入为该组别的全部槽位；结构或身份不符时以固定代码拒绝。"""
    try:
        return _import(job_dir, plan, arm_id, parsers, evidence_root, source_timezone)
    except EvalkitError as error:
        if error.code != "file_invalid":
            raise
        raise _source_invalid() from None
    except (KeyError, TypeError, ValueError, OSError, RecursionError):
        raise _source_invalid() from None


def _source_invalid() -> EvalkitError:
    return EvalkitError("harbor_source_invalid", "Harbor源证据缺失、身份不一致或格式无效")


def _import(
    job_dir: Path,
    plan: RunPlan,
    arm_id: str,
    parsers: Mapping[str, HarborResultParser],
    evidence_root: Path,
    source_timezone: str | None,
) -> tuple[TrialRecord, ...]:
    if not job_dir.resolve(strict=True).is_relative_to(evidence_root.resolve(strict=True)):
        raise EvalkitError("harbor_evidence_root_invalid", "Harbor Job目录必须位于证据库根目录内")
    arm = _harbor_arm(plan, arm_id)
    lock = read_json_object(job_dir / "lock.json", _MAX_SOURCE_BYTES)
    job = read_json_object(job_dir / "result.json", _MAX_SOURCE_BYTES)
    _require_supported_job(lock, job)
    trial_locks = lock["trials"]
    if not isinstance(trial_locks, list) or job["n_total_trials"] != len(trial_locks):
        raise ValueError("Harbor冻结槽位与运行总数不一致")
    slots = _bind_slots(trial_locks, plan, arm_id)
    created_at, finished_at = (
        _time(lock["created_at"], source_timezone),
        _time(job["finished_at"], source_timezone),
    )
    if created_at is None or finished_at is None or finished_at < created_at:
        raise ValueError("Harbor运行窗口无效")
    context = _JobContext(
        plan=plan,
        arm=arm,
        job_id=UUID(job["id"]),
        created_at=created_at,
        finished_at=finished_at,
        parsers=parsers,
        evidence_root=evidence_root,
        source_timezone=source_timezone,
    )
    collected = _collect_results(job_dir, trial_locks, context.job_id)
    trials = [
        _record(slots[index], trial_lock, *collected[index], context)
        if index in collected
        else _missing(slots[index], trial_lock, context)
        for index, trial_lock in enumerate(trial_locks)
    ]
    _require_unique_refs(trials, parsers)
    return tuple(sorted(trials, key=lambda trial: trial.slot))


# ---------------------------------------------------------------- 奖励判分器


class HarborRewardGrader:
    """读取冻结的Harbor结果并核对摘要；奖励缺失时不产出分数，由主指标判为invalid。"""

    def __init__(self, spec: GraderSpec, reward_key: str = "reward", pass_reward: float = 1.0):
        if spec.pass_score == "reward":
            raise ValueError("通过分数名不能与原始奖励分数同名")
        self.spec = spec
        self.reward_key = reward_key
        self.pass_reward = pass_reward

    def grade(self, plan: RunPlan, trial: TrialRecord, evidence_root: Path) -> tuple[Score, ...]:
        del plan
        ref: ArtifactRef | None = next(
            (a for a in trial.artifacts if a.name == RESULT_ARTIFACT), None
        )
        if ref is None:
            return ()
        try:
            result = json.loads(read_artifact(ref, evidence_root, _MAX_SOURCE_BYTES))
            rewards = _object(_object(_object(result).get("verifier_result")).get("rewards"))
        except (ValueError, RecursionError):
            raise EvalkitError("harbor_result_invalid", "Harbor结果制品无效") from None
        value = rewards.get(self.reward_key)
        if value is None:
            return ()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise EvalkitError("harbor_result_invalid", "Harbor奖励不是有限数值")
        reward = float(value)
        grader_id = self.spec.grader_id
        return (
            Score(name=self.spec.pass_score, value=reward == self.pass_reward, grader_id=grader_id),
            Score(name="reward", value=reward, grader_id=grader_id),
        )
