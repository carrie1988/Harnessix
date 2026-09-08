"""以人工语义Oracle评测Compaction候选，不调用模型裁判。"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.context.compaction import PreparedCompaction, ValidatedCompaction
from harnessix.context.tool_result_view import history_document
from harnessix.evals.compaction_contracts import (
    CompactionSemanticCheck,
    CompactionSemanticEvalCase,
    CompactionSemanticEvalReport,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def grade_compaction_semantics(
    prepared: PreparedCompaction,
    candidate: ValidatedCompaction,
    case: CompactionSemanticEvalCase,
) -> CompactionSemanticEvalReport:
    """对同一已验证候选执行可复现、无正文落盘的语义保持检查。"""

    try:
        case = CompactionSemanticEvalCase.model_validate_json(case.model_dump_json())
    except ValueError:
        raise KernelError("compaction_eval_case_invalid", "Compaction语义评测用例无效") from None
    if (
        prepared.plan != candidate.plan
        or prepared.plan.compaction_id != candidate.summary.compaction_id
    ):
        raise KernelError("compaction_eval_evidence_mismatch", "Compaction评测证据不属于同一候选")

    source = prepared.summary_source
    summary = candidate.summary.text
    history = "[" + ",".join(history_document(item) for item in candidate.history) + "]"
    checks = tuple(
        CompactionSemanticCheck(
            expectation_id=expectation.expectation_id,
            category=expectation.category,
            source_bound=all(marker in source for marker in expectation.source_markers),
            preserved=any(phrase in summary for phrase in expectation.accepted_summary_phrases),
        )
        for expectation in case.expectations
    )
    corpus_bound = all(check.source_bound for check in checks) and all(
        marker in source for marker in case.omitted_history_markers
    )
    forbidden_clean = all(phrase not in summary for phrase in case.forbidden_summary_phrases)
    covered_omitted = all(marker not in history for marker in case.omitted_history_markers)
    failed = tuple(check.expectation_id for check in checks if not check.preserved)
    outcome: Literal["passed", "failed", "invalid"] = (
        "invalid"
        if not corpus_bound
        else "passed"
        if not failed and forbidden_clean and covered_omitted
        else "failed"
    )
    plan_body = json.dumps(
        prepared.plan.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CompactionSemanticEvalReport(
        case_id=case.case_id,
        compaction_id=prepared.plan.compaction_id,
        outcome=outcome,
        plan_sha256=_sha(plan_body),
        source_sha256=prepared.plan.summary_source_sha256,
        summary_sha256=candidate.summary_sha256,
        history_sha256=candidate.history_sha256,
        history_tokens=candidate.history_tokens,
        corpus_bound=corpus_bound,
        forbidden_summary_clean=forbidden_clean,
        covered_history_omitted=covered_omitted,
        checks=checks,
        failed_expectation_ids=failed,
    )
