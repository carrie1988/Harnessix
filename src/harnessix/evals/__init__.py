"""可复现 Coding Eval 的任务、证据和报告契约。"""

from harnessix.evals.campaign import (
    CompletedCodingEvalTrial,
    build_coding_eval_campaign_report,
)
from harnessix.evals.campaign_contracts import (
    CodingEvalCampaignPlan,
    CodingEvalCampaignReport,
    CodingEvalCampaignSummary,
    CodingEvalCampaignTrial,
)
from harnessix.evals.campaign_execution import run_coding_eval_campaign
from harnessix.evals.campaign_execution_contracts import (
    CodingEvalCampaignExecutionState,
    CodingEvalCampaignRunConfig,
    CodingEvalCampaignRunReport,
)
from harnessix.evals.catalog import (
    HistoricalCodingEval,
    historical_coding_eval,
    historical_coding_eval_ids,
    historical_coding_eval_versions,
)
from harnessix.evals.checks import (
    historical_check_arguments,
    historical_python_launcher,
    run_historical_checks,
)
from harnessix.evals.compaction import grade_compaction_semantics
from harnessix.evals.compaction_contracts import (
    COMPACTION_SEMANTIC_CATEGORIES,
    CompactionSemanticCheck,
    CompactionSemanticEvalCase,
    CompactionSemanticEvalReport,
    CompactionSemanticExpectation,
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
from harnessix.evals.delivery import (
    CodingEvalDeliveryStore,
    build_coding_eval_change_package,
    read_coding_eval_change_package,
    write_coding_eval_change_package,
)
from harnessix.evals.delivery_contracts import (
    CHANGE_PACKAGE_SPEC_VERSION,
    DELIVERY_PLAN_SPEC_VERSION,
    DELIVERY_RECORD_SPEC_VERSION,
    CodingEvalChangeImage,
    CodingEvalChangePackage,
    CodingEvalDeliveryPlan,
    CodingEvalDeliveryRecord,
    CodingEvalDeliveryTransition,
)
from harnessix.evals.git_evidence import collect_git_evidence
from harnessix.evals.grader import grade_coding_eval
from harnessix.evals.materializer import (
    MaterializedCodingEval,
    load_materialized_coding_eval,
    materialize_historical_coding_eval,
)
from harnessix.evals.report import (
    read_eval_campaign_execution_state,
    read_eval_campaign_plan,
    read_eval_campaign_report,
    read_eval_report,
    write_eval_campaign_execution_state,
    write_eval_campaign_plan,
    write_eval_campaign_report,
    write_eval_report,
)
from harnessix.evals.run_state import read_eval_run_state, write_eval_run_state
from harnessix.evals.runner import HistoricalCodingEvalResult, run_historical_coding_eval

__all__ = [
    "CODING_EVAL_GRADER_VERSION",
    "CODING_EVAL_MATERIALIZER_VERSION",
    "CODING_EVAL_SPEC_VERSION",
    "COMPACTION_SEMANTIC_CATEGORIES",
    "CHANGE_PACKAGE_SPEC_VERSION",
    "DELIVERY_PLAN_SPEC_VERSION",
    "DELIVERY_RECORD_SPEC_VERSION",
    "CodingEvalChangeImage",
    "CodingEvalChangePackage",
    "CodingEvalCampaignPlan",
    "CodingEvalCampaignReport",
    "CodingEvalCampaignExecutionState",
    "CodingEvalCampaignRunConfig",
    "CodingEvalCampaignRunReport",
    "CodingEvalCampaignSummary",
    "CodingEvalCampaignTrial",
    "CodingEvalEnvironment",
    "CodingEvalDeliveryPlan",
    "CodingEvalDeliveryRecord",
    "CodingEvalDeliveryStore",
    "CodingEvalDeliveryTransition",
    "CodingEvalMaterialization",
    "CodingEvalReport",
    "CodingEvalRunState",
    "CodingEvalTask",
    "CompactionSemanticCheck",
    "CompactionSemanticEvalCase",
    "CompactionSemanticEvalReport",
    "CompactionSemanticExpectation",
    "CompletedCodingEvalTrial",
    "EvalFinalAnswer",
    "EvalFinalAnswerEvidence",
    "EvalFinalTest",
    "EvalGitEvidence",
    "EvalTestObservation",
    "HistoricalCodingEval",
    "HistoricalCodingEvalResult",
    "MaterializedCodingEval",
    "collect_git_evidence",
    "build_coding_eval_campaign_report",
    "build_coding_eval_change_package",
    "grade_coding_eval",
    "grade_compaction_semantics",
    "historical_check_arguments",
    "historical_python_launcher",
    "historical_coding_eval",
    "historical_coding_eval_ids",
    "historical_coding_eval_versions",
    "load_materialized_coding_eval",
    "materialize_historical_coding_eval",
    "read_eval_report",
    "read_eval_campaign_plan",
    "read_eval_campaign_report",
    "read_eval_campaign_execution_state",
    "read_eval_run_state",
    "read_coding_eval_change_package",
    "run_historical_checks",
    "run_historical_coding_eval",
    "run_coding_eval_campaign",
    "write_eval_report",
    "write_eval_campaign_plan",
    "write_eval_campaign_report",
    "write_eval_campaign_execution_state",
    "write_eval_run_state",
    "write_coding_eval_change_package",
]
