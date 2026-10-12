"""SWE-bench 预注册计划与本地数据导出的有界读写。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.evals.swebench.files import read_regular, write_atomic

_MAX_DATASET_BYTES = 256 * 1024 * 1024
_MAX_DATASET_ROWS = 10_000
_MAX_DATASET_LINE_BYTES = 2 * 1024 * 1024
_MAX_PLAN_BYTES = 16 * 1024 * 1024


def read_dataset_jsonl(path: Path) -> tuple[Mapping[str, object], ...]:
    """读取官方数据导出的 JSONL；黄金字段只停留在该瞬时 evaluator-side 对象中。"""

    body = read_regular(
        path,
        _MAX_DATASET_BYTES,
        error_code="swebench_dataset_invalid",
        label="SWE-bench 数据导出",
    )
    rows: list[Mapping[str, object]] = []
    try:
        for line in body.splitlines():
            if not line or len(line) > _MAX_DATASET_LINE_BYTES:
                raise ValueError
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError
            rows.append(value)
            if len(rows) > _MAX_DATASET_ROWS:
                raise ValueError
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise KernelError("swebench_dataset_invalid", "SWE-bench 数据导出无效") from None
    if not rows:
        raise KernelError("swebench_dataset_invalid", "SWE-bench 数据导出为空")
    return tuple(rows)


def write_preregistration_plan(path: Path, plan: SweBenchPreRegistrationPlan) -> None:
    """先严格重验摘要，再原子发布可提交仓库的公开预注册计划。"""

    try:
        checked = SweBenchPreRegistrationPlan.model_validate_json(
            plan.model_dump_json(), strict=True
        )
    except ValueError:
        raise KernelError("swebench_plan_invalid", "SWE-bench 预注册计划无效") from None
    body = (checked.model_dump_json(indent=2) + "\n").encode("utf-8")
    if len(body) > _MAX_PLAN_BYTES:
        raise KernelError("swebench_plan_too_large", "SWE-bench 预注册计划超过大小上限")
    write_atomic(path, body, error_code="swebench_write_failed", label="SWE-bench 预注册计划")


def read_preregistration_plan(path: Path) -> SweBenchPreRegistrationPlan:
    body = read_regular(
        path,
        _MAX_PLAN_BYTES,
        error_code="swebench_plan_invalid",
        label="SWE-bench 预注册计划",
    )
    try:
        return SweBenchPreRegistrationPlan.model_validate_json(body, strict=True)
    except ValueError:
        raise KernelError("swebench_plan_invalid", "SWE-bench 预注册计划无效") from None
