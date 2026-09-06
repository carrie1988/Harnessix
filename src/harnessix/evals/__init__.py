"""可复现 Coding Eval 的任务、证据和报告契约。"""

from harnessix.evals.contracts import (
    CODING_EVAL_GRADER_VERSION,
    CODING_EVAL_SPEC_VERSION,
    CodingEvalEnvironment,
    CodingEvalReport,
    CodingEvalTask,
    EvalFinalAnswer,
    EvalFinalAnswerEvidence,
    EvalFinalTest,
    EvalGitEvidence,
    EvalTestObservation,
)
from harnessix.evals.git_evidence import collect_git_evidence
from harnessix.evals.grader import grade_coding_eval
from harnessix.evals.report import read_eval_report, write_eval_report

__all__ = [
    "CODING_EVAL_GRADER_VERSION",
    "CODING_EVAL_SPEC_VERSION",
    "CodingEvalEnvironment",
    "CodingEvalReport",
    "CodingEvalTask",
    "EvalFinalAnswer",
    "EvalFinalAnswerEvidence",
    "EvalFinalTest",
    "EvalGitEvidence",
    "EvalTestObservation",
    "collect_git_evidence",
    "grade_coding_eval",
    "read_eval_report",
    "write_eval_report",
]
