"""经源码历史求证的内置 Coding Eval 任务目录。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget
from harnessix.evals.contracts import CodingEvalTask, EvalRepository


@dataclass(frozen=True, slots=True)
class HistoricalCheck:
    check_id: str
    mode: Literal["empty_id_behavior", "identity_guards"]


@dataclass(frozen=True, slots=True)
class HistoricalCodingEval:
    task: CodingEvalTask
    source_tree_oid: str
    host_only_paths: tuple[str, ...]
    checks: tuple[HistoricalCheck, ...]

    def check(self, check_id: str) -> HistoricalCheck:
        for check in self.checks:
            if check.check_id == check_id:
                return check
        raise KernelError("eval_check_not_found", "历史任务检查定义不存在")


_EMPTY_INCREMENTAL_CALL_ID = HistoricalCodingEval(
    task=CodingEvalTask(
        task_id="harnessix-openai-empty-incremental-call-id",
        task_version=1,
        repository=EvalRepository(
            name="Harnessix",
            origin="https://github.com/carrie1988/Harnessix",
            source_revision="9f24961840fa704e7c7a344c648164d8afe793b7",
            baseline_tree_sha256=(
                "d91bdff8b78e85222f16e00b44b6888c563c2986f27ca831b414930ec2623ba4"
            ),
        ),
        prompt=(
            "修复OpenAI-compatible流式工具调用兼容缺陷：首个分片提供非空调用ID，"
            "后续分片可能用空字符串表示没有新增身份。空占位不应触发身份漂移；"
            "真实非空ID、工具名称或类型漂移仍必须拒绝。先运行focused测试，"
            "只修改允许的实现文件，测试通过后核对Git状态和差异，并按约定输出JSON。"
        ),
        allowed_changed_paths=("src/harnessix/models/_chat_stream.py",),
        required_test_profiles=("focused",),
        baseline_checks=("empty-id-behavior",),
        behavior_checks=("empty-id-behavior",),
        regression_checks=("identity-guards",),
        max_changed_files=1,
        budget=Budget(
            max_steps=16,
            max_tokens=20_000,
            max_output_chars=65_536,
            max_tool_calls_per_step=8,
            timeout_seconds=600,
        ),
    ),
    source_tree_oid="c3df320a023537c1e0a6931a758ac67940279658",
    host_only_paths=(".env.example",),
    checks=(
        HistoricalCheck("empty-id-behavior", "empty_id_behavior"),
        HistoricalCheck("identity-guards", "identity_guards"),
    ),
)

_TASKS = {_EMPTY_INCREMENTAL_CALL_ID.task.task_id: _EMPTY_INCREMENTAL_CALL_ID}


def historical_coding_eval(task_id: str) -> HistoricalCodingEval:
    try:
        return _TASKS[task_id]
    except (KeyError, TypeError):
        raise KernelError("eval_task_not_found", "历史 Coding Eval 任务不存在") from None


def historical_coding_eval_ids() -> tuple[str, ...]:
    return tuple(sorted(_TASKS))
