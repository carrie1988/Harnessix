"""只使用公开身份字段的确定性 SWE-bench 选样。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping

from harnessix.evals.swebench.contracts import (
    SWE_BENCH_CASE_COUNT,
    SWE_BENCH_SELECTION_POLICY,
    SweBenchPublicCase,
    SweBenchSelectionPolicy,
)

_SELECTION_DOMAIN = b"harnessix.swebench.selection/v1\x00"


def _selection_key(salt: str, kind: str, *identity: str) -> bytes:
    body = json.dumps(
        [salt, kind, *identity],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_SELECTION_DOMAIN + body).digest()


def _validate_selection_inputs(salt: str, selection_policy: str) -> None:
    if type(salt) is not str or not 16 <= len(salt) <= 256 or any(
        ord(character) < 32 or ord(character) == 127 for character in salt
    ):
        raise ValueError("SWE-bench selection salt is invalid")
    if selection_policy != SWE_BENCH_SELECTION_POLICY:
        raise ValueError("SWE-bench selection policy is unsupported")


def _public_case(row: Mapping[str, object] | SweBenchPublicCase) -> SweBenchPublicCase:
    if type(row) is SweBenchPublicCase:
        return row
    if not isinstance(row, Mapping):
        raise ValueError("SWE-bench candidate row must be a mapping")
    public = {name: row[name] for name in SweBenchPublicCase.model_fields if name in row}
    return SweBenchPublicCase.model_validate(public, strict=True)


def project_swebench_public_cases(
    rows: Iterable[Mapping[str, object] | SweBenchPublicCase],
) -> tuple[SweBenchPublicCase, ...]:
    """投影并按 instance_id 冻结完整候选总体，拒绝重复身份。"""

    cases = tuple(sorted((_public_case(row) for row in rows), key=lambda case: case.instance_id))
    instance_ids = [case.instance_id for case in cases]
    if len(instance_ids) != len(set(instance_ids)):
        raise ValueError("SWE-bench candidate instance_id values must be unique")
    return cases


def canonical_swebench_case_order(
    cases: Iterable[SweBenchPublicCase],
    *,
    salt: str,
    selection_policy: SweBenchSelectionPolicy = SWE_BENCH_SELECTION_POLICY,
) -> tuple[SweBenchPublicCase, ...]:
    """用带盐公开身份排序仓库分层及各层 Case。"""

    _validate_selection_inputs(salt, selection_policy)
    by_repo: dict[str, list[SweBenchPublicCase]] = defaultdict(list)
    for case in cases:
        by_repo[case.repo].append(case)
    repos = sorted(
        by_repo,
        key=lambda repo: (_selection_key(salt, "repo", repo), repo),
    )
    for repo_cases in by_repo.values():
        repo_cases.sort(
            key=lambda case: (
                _selection_key(
                    salt,
                    "case",
                    case.repo,
                    case.instance_id,
                    case.base_commit,
                ),
                case.instance_id,
                case.base_commit,
            )
        )

    ordered: list[SweBenchPublicCase] = []
    round_index = 0
    while len(ordered) < sum(len(repo_cases) for repo_cases in by_repo.values()):
        for repo in repos:
            repo_cases = by_repo[repo]
            if round_index < len(repo_cases):
                ordered.append(repo_cases[round_index])
        round_index += 1
    return tuple(ordered)


def select_swebench_cases(
    rows: Iterable[Mapping[str, object] | SweBenchPublicCase],
    *,
    salt: str,
    selection_policy: SweBenchSelectionPolicy = SWE_BENCH_SELECTION_POLICY,
) -> tuple[SweBenchPublicCase, ...]:
    """投影到公开字段白名单后，选择恰好 25 个唯一 Case。"""

    _validate_selection_inputs(salt, selection_policy)
    cases = project_swebench_public_cases(rows)
    if len(cases) < SWE_BENCH_CASE_COUNT:
        raise ValueError(f"SWE-bench selection requires at least {SWE_BENCH_CASE_COUNT} candidates")
    return canonical_swebench_case_order(
        cases,
        salt=salt,
        selection_policy=selection_policy,
    )[:SWE_BENCH_CASE_COUNT]
