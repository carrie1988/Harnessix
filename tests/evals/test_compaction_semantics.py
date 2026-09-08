from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.context.compaction import validate_compaction
from harnessix.context.compaction_contracts import CompactionSummary
from harnessix.evals.compaction import grade_compaction_semantics
from harnessix.evals.compaction_contracts import (
    CompactionSemanticEvalCase,
    CompactionSemanticExpectation,
)
from tests.context.test_compaction import plan, text, thread_with, tool_group

RAW_MARKER = "RAW_TRACE_BLOCK_7f91"


def semantic_case(**updates: object) -> CompactionSemanticEvalCase:
    facts = (
        ("01-objective", "objective", "修复增量解析器", "目标：修复增量解析器"),
        ("02-constraint", "constraint", "保持公开 API 兼容", "约束：保持公开 API 兼容"),
        ("03-unresolved", "unresolved_work", "验证断线恢复", "未完成：验证断线恢复"),
        (
            "04-file-revision",
            "file_revision",
            "src/parser.py@abc123",
            "src/parser.py 基于 revision abc123",
        ),
        ("05-test", "test_result", "test_streaming=failed", "test_streaming 当前失败"),
        (
            "06-effect",
            "effect_uncertainty",
            "patch_persistence=unknown",
            "补丁是否落盘尚不确定",
        ),
    )
    values: dict[str, object] = {
        "case_id": "parser-recovery-v1",
        "expectations": tuple(
            CompactionSemanticExpectation(
                expectation_id=identity,
                category=category,
                source_markers=(source,),
                accepted_summary_phrases=(accepted,),
            )
            for identity, category, source, accepted in facts
        ),
        "forbidden_summary_phrases": ("所有测试已通过",),
        "omitted_history_markers": (RAW_MARKER,),
    }
    values.update(updates)
    return CompactionSemanticEvalCase.model_validate(values)


def summary_text() -> str:
    return (
        "目标：修复增量解析器；约束：保持公开 API 兼容；"
        "未完成：验证断线恢复；src/parser.py 基于 revision abc123；"
        "test_streaming 当前失败；补丁是否落盘尚不确定。"
    )


async def evidence(content: str | None = None):
    source = (
        "修复增量解析器；保持公开 API 兼容；验证断线恢复；"
        "src/parser.py@abc123；test_streaming=failed；patch_persistence=unknown；"
        + (RAW_MARKER + "\n")
        * 200
    )
    thread = thread_with(
        (
            text("继续生产故障修复", "user_message"),
            *tool_group(count=1, output=source),
            text("根据既有证据继续处理。"),
        )
    )
    prepared = await plan(thread)
    summary = CompactionSummary(
        compaction_id=prepared.plan.compaction_id,
        text=content or summary_text(),
    )
    candidate = await validate_compaction(thread, prepared.plan, summary, CancelToken())
    return prepared, candidate


async def test_realistic_engineering_oracle_accepts_preserved_semantics_without_raw_body():
    prepared, candidate = await evidence()
    report = grade_compaction_semantics(prepared, candidate, semantic_case())

    assert report.outcome == "passed"
    assert report.corpus_bound and report.forbidden_summary_clean
    assert report.covered_history_omitted and not report.failed_expectation_ids
    assert {check.category for check in report.checks} == {
        "objective",
        "constraint",
        "unresolved_work",
        "file_revision",
        "test_result",
        "effect_uncertainty",
    }
    serialized = report.model_dump_json()
    assert RAW_MARKER not in serialized
    assert "保持公开 API 兼容" not in serialized
    assert candidate.summary.text not in serialized


@pytest.mark.parametrize(
    ("content", "outcome", "failed", "forbidden_clean"),
    [
        (
            summary_text().replace("未完成：验证断线恢复；", ""),
            "failed",
            ("03-unresolved",),
            True,
        ),
        (summary_text() + "所有测试已通过", "failed", (), False),
    ],
)
async def test_missing_fact_and_hallucinated_claim_fail_closed(
    content: str,
    outcome: str,
    failed: tuple[str, ...],
    forbidden_clean: bool,
):
    prepared, candidate = await evidence(content)
    report = grade_compaction_semantics(prepared, candidate, semantic_case())
    assert report.outcome == outcome
    assert report.failed_expectation_ids == failed
    assert report.forbidden_summary_clean is forbidden_clean


async def test_oracle_not_bound_to_source_is_invalid_instead_of_task_failure():
    prepared, candidate = await evidence()
    current = semantic_case()
    first = current.expectations[0].model_copy(update={"source_markers": ("不存在的事实",)})
    case = current.model_copy(update={"expectations": (first, *current.expectations[1:])})
    report = grade_compaction_semantics(prepared, candidate, case)
    assert report.outcome == "invalid"
    assert not report.corpus_bound
    assert not report.checks[0].source_bound


def test_semantic_case_is_strict_complete_and_deterministic():
    current = semantic_case()
    assert CompactionSemanticEvalCase.model_validate_json(current.model_dump_json()) == current
    with pytest.raises(ValidationError):
        semantic_case(expectations=current.expectations[:-1])
    with pytest.raises(ValidationError):
        CompactionSemanticEvalCase.model_validate(
            {**current.model_dump(), "forbidden_summary_phrases": ["b", "a"]}
        )


async def test_candidate_identity_mismatch_is_rejected():
    prepared, candidate = await evidence()
    with pytest.raises(KernelError) as error:
        grade_compaction_semantics(
            replace(
                prepared,
                plan=prepared.plan.model_copy(update={"compaction_id": candidate.plan.thread_id}),
            ),
            candidate,
            semantic_case(),
        )
    assert error.value.code == "compaction_eval_evidence_mismatch"
