"""可复现 Coding Eval 的任务、证据和报告契约。"""

from harnessix.evals.catalog import (
    HistoricalCodingEval,
    historical_coding_eval,
    historical_coding_eval_ids,
)
from harnessix.evals.checks import (
    historical_check_arguments,
    historical_python_launcher,
    run_historical_checks,
)
from harnessix.evals.contracts import (
    CODING_EVAL_GRADER_VERSION,
    CODING_EVAL_MATERIALIZER_VERSION,
    CODING_EVAL_SPEC_VERSION,
    CodingEvalEnvironment,
    CodingEvalMaterialization,
    CodingEvalReport,
    CodingEvalRunState,
    CodingEvalTask,
    EvalFinalAnswer,
    EvalFinalAnswerEvidence,
    EvalFinalTest,
    EvalGitEvidence,
    EvalTestObservation,
)
from harnessix.evals.git_evidence import collect_git_evidence
from harnessix.evals.grader import grade_coding_eval
from harnessix.evals.materializer import (
    MaterializedCodingEval,
    load_materialized_coding_eval,
    materialize_historical_coding_eval,
)
from harnessix.evals.report import read_eval_report, write_eval_report
from harnessix.evals.run_state import read_eval_run_state, write_eval_run_state
from harnessix.evals.runner import HistoricalCodingEvalResult, run_historical_coding_eval

__all__ = [
    "CODING_EVAL_GRADER_VERSION",
    "CODING_EVAL_MATERIALIZER_VERSION",
    "CODING_EVAL_SPEC_VERSION",
    "CodingEvalEnvironment",
    "CodingEvalMaterialization",
    "CodingEvalReport",
    "CodingEvalRunState",
    "CodingEvalTask",
    "EvalFinalAnswer",
    "EvalFinalAnswerEvidence",
    "EvalFinalTest",
    "EvalGitEvidence",
    "EvalTestObservation",
    "HistoricalCodingEval",
    "HistoricalCodingEvalResult",
    "MaterializedCodingEval",
    "collect_git_evidence",
    "grade_coding_eval",
    "historical_check_arguments",
    "historical_python_launcher",
    "historical_coding_eval",
    "historical_coding_eval_ids",
    "load_materialized_coding_eval",
    "materialize_historical_coding_eval",
    "read_eval_report",
    "read_eval_run_state",
    "run_historical_checks",
    "run_historical_coding_eval",
    "write_eval_report",
    "write_eval_run_state",
]
