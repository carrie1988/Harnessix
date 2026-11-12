"""evalkit测试共用的计划与Harbor Job夹具；只生成结构化外部证据，不运行容器。"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from evalkit.contracts import ArmSpec, DatasetItem, DatasetManifest, GraderSpec, RunPlan

PLAN_CREATED = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
CONFIG = "sha256:" + "c" * 64
HARBOR_REWARD = GraderSpec(grader_id="harbor-reward", version="0.25.0", pass_score="reward_passed")


def task_digest(index: int) -> str:
    return "sha256:" + format(index + 1, "x") * 64


@dataclass(frozen=True)
class Trial:
    """一个已执行试次的外部事实；metadata按试次ID生成智能体自报的结构化摘要。"""

    reward: float | None = 1.0
    exception: str | None = None
    metadata: Callable[[str], dict[str, Any]] | None = None


def harbor_job(
    root: Path,
    trials: Sequence[Trial | None],
    *,
    agent_name: str = "fixture-agent",
    attempts: int = 1,
) -> Path:
    """按Harbor 0.25.0的Job目录形状写冻结锁与结果；None表示该任务没有结果目录。"""
    root.mkdir(parents=True)
    job_id = str(uuid4())
    locks = []
    for index, spec in enumerate(trials):
        lock = {
            "schema_version": 2,
            "task": {"name": f"case-{index}", "digest": task_digest(index)},
            "agent": {"import_path": "agents.fixture:Agent", "kwargs": {}},
            "environment": {"type": "docker"},
            "verifier": {"environment_mode": "separate"},
        }
        for attempt in range(attempts):
            locks.append(lock)
            if spec is not None:
                _write_trial(root / f"trial-{index}-{attempt}", lock, spec, job_id, agent_name)
    (root / "lock.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "created_at": "2026-11-05T10:00:00Z",
                "harbor": {"version": "0.25.0"},
                "retry": {"max_retries": 0},
                "trials": locks,
            }
        )
    )
    (root / "result.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "n_total_trials": len(locks),
                "finished_at": "2026-11-05T10:30:00Z",
                "stats": {"n_retries": 0},
            }
        )
    )
    return root


def _write_trial(
    directory: Path, lock: dict[str, Any], spec: Trial, job_id: str, agent_name: str
) -> None:
    directory.mkdir()
    trial_id = str(uuid4())
    value = {
        "id": trial_id,
        "task_name": lock["task"]["name"],
        "trial_name": directory.name,
        "config": {"job_id": job_id, "agent": lock["agent"], "environment": lock["environment"]},
        "agent_info": {"name": agent_name, "version": "1.0.0"},
        "agent_result": {
            "metadata": spec.metadata(trial_id) if spec.metadata else {},
            "cost_usd": None,
            "n_input_tokens": None,
            "n_output_tokens": None,
        },
        "verifier_environment_mode": "separate",
        "verifier_result": {"rewards": {} if spec.reward is None else {"reward": spec.reward}},
        "exception_info": None
        if spec.exception is None
        else {
            "exception_type": spec.exception,
            "exception_message": "PRIVATE-EXCEPTION-MUST-NOT-EXPORT",
        },
        "started_at": "2026-11-05T10:01:00Z",
        "finished_at": "2026-11-05T10:02:00Z",
    }
    (directory / "lock.json").write_text(json.dumps(lock))
    (directory / "result.json").write_text(json.dumps(value))


def change_result(job: Path, mutate: Callable[[dict[str, Any]], None], trial: str) -> None:
    path = job / trial / "result.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data))


def harbor_plan(
    tasks: int,
    *,
    attempts: int = 1,
    system: str = "fixture-agent",
    graders: tuple[GraderSpec, ...] = (HARBOR_REWARD,),
    primary: str = "harbor-reward",
    created_at: datetime = PLAN_CREATED,
) -> RunPlan:
    return RunPlan(
        run_name="harbor-fixture",
        dataset=DatasetManifest(
            name="fixture",
            version="1",
            items=tuple(
                DatasetItem(item_id=f"case-{index}", item_digest=task_digest(index))
                for index in range(tasks)
            ),
        ),
        arms=(
            ArmSpec(
                arm_id="A",
                system=system,
                system_version="1.0.0",
                config_digest=CONFIG,
                executor="harbor",
            ),
        ),
        attempts_per_item=attempts,
        graders=graders,
        primary_grader=primary,
        created_at=created_at,
    )
