"""从多个独立候选中选出一个补丁：用候选自己验证过的检查命令交叉运行打分。

每个候选是一次完整、互相隔离的单Agent运行。选择只依据候选在运行中实际执行并通过的检查命令，
不接触隐藏测试或参考补丁；平局按相同补丁的出现次数、候选自身是否正常结束和补丁大小决定。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

CheckResult = Literal["passed", "failed", "error"]

# 只读查看类命令不区分补丁好坏，交叉运行只浪费时间。
_INSPECTION = re.compile(
    r"^\s*(?:cd\s+\S+\s*&&\s*)?(?:cat|ls|grep|rg|find|head|tail|sed\s+-n|wc|git|echo|pwd|which|"
    r"tree|stat|file|diff)\b"
)
# 测试、复现或运行代码的命令才有区分度。
_EXERCISES = re.compile(
    r"pytest|unittest|runtests|bin/test|tox\b|nox\b|python\d*(?:\.\d+)?\s|"
    r"\bgo\s+test|\bcargo\s+test|\bmvn\b|\bgradle|\bnpm\s+(?:run\s+)?test|\byarn\s+test|"
    r"\bnpx\s|\bjest\b|\bnode\s|\bruby\s|\brspec\b|\bphpunit\b|\bphp\s|\bmake\s+(?:test|check)",
)


@dataclass(frozen=True)
class CommandRecord:
    cmd: str
    outcome: str | None
    returncode: int | None


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    patch: str
    status: str | None
    commands: tuple[CommandRecord, ...] = ()

    @property
    def patch_sha256(self) -> str:
        return hashlib.sha256(self.patch.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Check:
    """一条交叉检查命令及提出它的候选（其草稿文件在交叉运行时一并提供）。"""

    cmd: str
    origin: str


@dataclass(frozen=True)
class Ranked:
    candidate_id: str
    passed: int
    duplicates: int
    completed: bool
    patch_bytes: int


@dataclass(frozen=True)
class Selection:
    selected: str | None
    checks: tuple[Check, ...]
    ranking: tuple[Ranked, ...]
    matrix: Mapping[str, Mapping[int, CheckResult]] = field(default_factory=dict)
    reason: str = ""


def is_discriminating(cmd: str) -> bool:
    return _EXERCISES.search(cmd) is not None and _INSPECTION.match(cmd) is None


def select_checks(candidates: Sequence[Candidate], *, limit: int = 8) -> tuple[Check, ...]:
    """每个候选最后成功运行过的有区分度命令；按候选轮流取，去重，最多limit条。

    只收在候选自己的补丁上返回0的命令：失败的命令不能说明“正确补丁应当通过”。
    """

    per_candidate: list[list[Check]] = []
    for candidate in candidates:
        if not candidate.patch:
            continue
        latest: dict[str, bool] = {}
        for record in candidate.commands:
            latest.pop(record.cmd, None)  # 重新插入，保留最后一次运行的顺序
            latest[record.cmd] = record.outcome == "succeeded" and record.returncode == 0
        checks = [
            Check(cmd, candidate.candidate_id)
            for cmd, passed in reversed(latest.items())
            if passed and is_discriminating(cmd)
        ]
        per_candidate.append(checks)
    selected: list[Check] = []
    seen: set[str] = set()
    for round_index in range(max((len(items) for items in per_candidate), default=0)):
        for checks in per_candidate:
            if round_index < len(checks) and checks[round_index].cmd not in seen:
                seen.add(checks[round_index].cmd)
                selected.append(checks[round_index])
                if len(selected) == limit:
                    return tuple(selected)
    return tuple(selected)


def rank(
    candidates: Sequence[Candidate],
    checks: Sequence[Check],
    matrix: Mapping[str, Mapping[int, CheckResult]],
) -> Selection:
    """按通过检查数、相同补丁数、是否正常结束、补丁更小依次排序；空补丁不参与。"""

    eligible = [candidate for candidate in candidates if candidate.patch]
    if not eligible:
        return Selection(None, tuple(checks), (), matrix, "没有非空补丁")
    duplicates: dict[str, int] = {}
    for candidate in eligible:
        duplicates[candidate.patch_sha256] = duplicates.get(candidate.patch_sha256, 0) + 1
    ranking = sorted(
        (
            Ranked(
                candidate_id=candidate.candidate_id,
                passed=sum(
                    1
                    for result in matrix.get(candidate.candidate_id, {}).values()
                    if result == "passed"
                ),
                duplicates=duplicates[candidate.patch_sha256],
                completed=candidate.status == "completed",
                patch_bytes=len(candidate.patch.encode("utf-8")),
            )
            for candidate in eligible
        ),
        key=lambda item: (-item.passed, -item.duplicates, not item.completed, item.patch_bytes),
    )
    best = ranking[0]
    reason = (
        f"通过{best.passed}/{len(checks)}条交叉检查，相同补丁{best.duplicates}个，"
        f"{'正常结束' if best.completed else '未正常结束'}，补丁{best.patch_bytes}字节"
    )
    return Selection(best.candidate_id, tuple(checks), tuple(ranking), matrix, reason)
