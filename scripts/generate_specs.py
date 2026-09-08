from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from harnessix.agent.models import (
    AgentEvent,
    CompactionWindow,
    Thread,
    ThreadArchiveRecord,
    ThreadForkSnapshot,
)
from harnessix.api import create_app
from harnessix.artifacts.contracts import (
    ArtifactPage,
    ArtifactPolicy,
    ArtifactRef,
    ReadArtifactInput,
)
from harnessix.context.compaction_contracts import (
    CompactionAnchor,
    CompactionPlan,
    CompactionPolicy,
    CompactionSummary,
)
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.contracts import (
    ContextConsistencySnapshot,
    ContextFragment,
    ContextInspection,
    ContextInspectionV3,
    ContextLimits,
    ContextSourceDocument,
    ContextSourceObservation,
    ContextSourceSnapshot,
)
from harnessix.context.tool_result_contracts import (
    ModelHistoryInspection,
    ModelHistoryInspectionV2,
    ToolResultViewDecision,
    ToolResultViewPolicy,
)
from harnessix.domain.models import ActionRequest
from harnessix.evals.campaign_contracts import (
    CodingEvalCampaignPlan,
    CodingEvalCampaignReport,
)
from harnessix.evals.campaign_execution_contracts import (
    CodingEvalCampaignExecutionState,
    CodingEvalCampaignRunConfig,
    CodingEvalCampaignRunReport,
)
from harnessix.evals.compaction_contracts import (
    CompactionSemanticEvalCase,
    CompactionSemanticEvalReport,
)
from harnessix.evals.contracts import (
    CodingEvalMaterialization,
    CodingEvalReport,
    CodingEvalRunState,
    CodingEvalTask,
    EvalFinalAnswer,
)
from harnessix.evals.delivery_contracts import (
    CodingEvalChangePackage,
    CodingEvalDeliveryPlan,
    CodingEvalDeliveryRecord,
)
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionCapabilityEvidence,
    ExecutionCapabilityEvidenceV2,
    ExecutionIntent,
    ExecutionPlan,
    ExecutionPlanV2,
)
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ProviderEvent
from harnessix.models.costs import CostReport, CostReportV2
from harnessix.models.pricing import PriceSnapshot
from harnessix.patches.batch_approval_contracts import (
    ManagedPatchBatchApproval,
    ManagedPatchBatchPlan,
)
from harnessix.patches.batch_bridge_contracts import (
    ManagedPatchBatchCallPlan,
    ManagedPatchBatchOutput,
)
from harnessix.patches.batch_contracts import PatchBatchManifest, PatchBatchProposal
from harnessix.patches.batch_run_contracts import BatchExecutionResult, BatchRunRecord
from harnessix.patches.bridge_contracts import ManagedPatchCallPlan, ManagedPatchOutput
from harnessix.patches.contracts import PatchManifest, PatchProposal
from harnessix.patches.diff_contracts import PatchBatchDiff, PatchDiffOptions
from harnessix.patches.diff_document_contracts import (
    BatchDiffDocument,
    BatchDiffDocumentOptions,
    BatchDiffRecord,
)
from harnessix.patches.managed_contracts import CopyManifest, PatchRecord
from harnessix.processes.bridge_contracts import AgentProcessCallPlan
from harnessix.processes.contracts import (
    ProcessLimits,
    ProcessRequest,
    ProcessResult,
    ProcessStream,
)
from harnessix.processes.output_artifact import ProcessOutputDocument, ProcessOutputRecord
from harnessix.processes.owner_protocol import ProcessOwnerCommand, ProcessOwnerStart
from harnessix.processes.owner_receipt import ProcessOwnerReceipt
from harnessix.processes.supervision_contracts import (
    ProcessCapabilityProbe,
    ProcessLaunchBinding,
    ProcessLease,
    ProcessOutputObservation,
    ProcessSpec,
)
from harnessix.sandbox.capabilities import ContainerEngineProbe, HostSandboxProbe
from harnessix.sandbox.contracts import (
    ContainerCommandSpec,
    ContainerExecutionSpec,
    ContainerSandboxProfile,
    ManagedEgressBinding,
    NetworkDestination,
    NetworkPolicy,
    NetworkPolicySnapshot,
    SandboxResourceLimits,
)
from harnessix.smoke.contracts import SmokeConfig, SmokeReport
from harnessix.tools.contracts import ListFilesInput, ListFilesOutput, ReadFileInput, ReadFileOutput
from harnessix.tools.search_contracts import (
    ArchivedGlobOutput,
    ArchivedGrepOutput,
    GlobInput,
    GlobOutput,
    GrepInput,
    GrepOutput,
)
from harnessix.workspace.contracts import WorkspaceLease, WorkspaceSnapshot


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    output = Path("spec")
    output.mkdir(exist_ok=True)
    write_json(output / "action-contract-v1.schema.json", ActionRequest.model_json_schema())
    write_json(output / "openapi.json", create_app().openapi())
    write_json(output / "agent-event-v17.schema.json", AgentEvent.model_json_schema())
    write_json(output / "agent-thread-v17.schema.json", Thread.model_json_schema())
    write_json(
        output / "context-inspection-v3.schema.json", ContextInspectionV3.model_json_schema()
    )
    write_json(output / "provider-event-v3.schema.json", TypeAdapter(ProviderEvent).json_schema())
    write_json(output / "openai-chat-config-v1.schema.json", OpenAIChatConfig.model_json_schema())
    write_json(output / "anthropic-config-v1.schema.json", AnthropicConfig.model_json_schema())
    write_json(output / "price-snapshot-v1.schema.json", PriceSnapshot.model_json_schema())
    write_json(output / "cost-report-v1.schema.json", CostReport.model_json_schema())
    write_json(output / "cost-report-v2.schema.json", CostReportV2.model_json_schema())
    write_json(output / "model-smoke-config-v1.schema.json", SmokeConfig.model_json_schema())
    write_json(output / "model-smoke-report-v1.schema.json", SmokeReport.model_json_schema())
    write_json(
        output / "batch-diff-record-v1.schema.json", TypeAdapter(BatchDiffRecord).json_schema()
    )
    write_json(
        output / "process-output-record-v1.schema.json",
        TypeAdapter(ProcessOutputRecord).json_schema(),
    )
    for name, model in (
        ("compaction-anchor", CompactionAnchor),
        ("compaction-plan", CompactionPlan),
        ("compaction-policy", CompactionPolicy),
        ("compaction-runtime", CompactionRuntimeConfig),
        ("compaction-summary", CompactionSummary),
        ("compaction-window", CompactionWindow),
        ("thread-fork", ThreadForkSnapshot),
        ("thread-archive", ThreadArchiveRecord),
        ("model-history-inspection", ModelHistoryInspection),
        ("tool-result-view-policy", ToolResultViewPolicy),
        ("tool-result-view-decision", ToolResultViewDecision),
        ("context-fragment", ContextFragment),
        ("context-limits", ContextLimits),
        ("context-inspection", ContextInspection),
        ("context-consistency", ContextConsistencySnapshot),
        ("context-source-document", ContextSourceDocument),
        ("context-source-observation", ContextSourceObservation),
        ("context-source-snapshot", ContextSourceSnapshot),
        ("agent-process-call-plan", AgentProcessCallPlan),
        ("process-request", ProcessRequest),
        ("process-limits", ProcessLimits),
        ("process-stream", ProcessStream),
        ("process-result", ProcessResult),
        ("process-output-document", ProcessOutputDocument),
        ("process-spec", ProcessSpec),
        ("process-capability", ProcessCapabilityProbe),
        ("process-launch-binding", ProcessLaunchBinding),
        ("process-lease", ProcessLease),
        ("process-output-observation", ProcessOutputObservation),
        ("process-owner-start", ProcessOwnerStart),
        ("process-owner-command", ProcessOwnerCommand),
        ("process-owner-receipt", ProcessOwnerReceipt),
        ("list-files-input", ListFilesInput),
        ("list-files-output", ListFilesOutput),
        ("read-file-input", ReadFileInput),
        ("read-file-output", ReadFileOutput),
        ("glob-input", GlobInput),
        ("glob-output", GlobOutput),
        ("grep-input", GrepInput),
        ("grep-output", GrepOutput),
        ("artifact-ref", ArtifactRef),
        ("artifact-policy", ArtifactPolicy),
        ("read-artifact-input", ReadArtifactInput),
        ("read-artifact-output", ArtifactPage),
        ("archived-glob-output", ArchivedGlobOutput),
        ("archived-grep-output", ArchivedGrepOutput),
        ("patch-proposal", PatchProposal),
        ("patch-manifest", PatchManifest),
        ("managed-copy-manifest", CopyManifest),
        ("managed-patch-record", PatchRecord),
        ("managed-patch-call-plan", ManagedPatchCallPlan),
        ("managed-patch-output", ManagedPatchOutput),
        ("patch-batch-proposal", PatchBatchProposal),
        ("patch-batch-manifest", PatchBatchManifest),
        ("patch-batch-diff", PatchBatchDiff),
        ("patch-diff-options", PatchDiffOptions),
        ("batch-diff-document", BatchDiffDocument),
        ("batch-diff-document-options", BatchDiffDocumentOptions),
        ("managed-patch-batch-plan", ManagedPatchBatchPlan),
        ("managed-patch-batch-approval", ManagedPatchBatchApproval),
        ("managed-patch-batch-call-plan", ManagedPatchBatchCallPlan),
        ("managed-patch-batch-output", ManagedPatchBatchOutput),
        ("managed-patch-batch-run", BatchRunRecord),
        ("managed-patch-batch-result", BatchExecutionResult),
        ("coding-eval-task", CodingEvalTask),
        ("coding-eval-final-answer", EvalFinalAnswer),
        ("coding-eval-report", CodingEvalReport),
        ("coding-eval-materialization", CodingEvalMaterialization),
        ("coding-eval-run-state", CodingEvalRunState),
        ("coding-eval-campaign-plan", CodingEvalCampaignPlan),
        ("coding-eval-campaign-report", CodingEvalCampaignReport),
        ("coding-eval-campaign-run-config", CodingEvalCampaignRunConfig),
        ("coding-eval-campaign-execution-state", CodingEvalCampaignExecutionState),
        ("coding-eval-campaign-run-report", CodingEvalCampaignRunReport),
        ("coding-eval-change-package", CodingEvalChangePackage),
        ("coding-eval-delivery-plan", CodingEvalDeliveryPlan),
        ("coding-eval-delivery-record", CodingEvalDeliveryRecord),
        ("compaction-semantic-eval-case", CompactionSemanticEvalCase),
        ("compaction-semantic-eval-report", CompactionSemanticEvalReport),
        ("workspace-snapshot", WorkspaceSnapshot),
        ("workspace-lease", WorkspaceLease),
        ("execution-intent", ExecutionIntent),
        ("execution-capability-evidence", ExecutionCapabilityEvidence),
        ("execution-plan", ExecutionPlan),
        ("execution-approval", ExecutionApprovalCheckpoint),
        ("network-destination", NetworkDestination),
        ("network-policy", NetworkPolicy),
        ("network-policy-snapshot", NetworkPolicySnapshot),
        ("sandbox-resource-limits", SandboxResourceLimits),
        ("container-sandbox-profile", ContainerSandboxProfile),
        ("container-command", ContainerCommandSpec),
        ("container-execution", ContainerExecutionSpec),
        ("managed-egress-binding", ManagedEgressBinding),
        ("container-engine-probe", ContainerEngineProbe),
        ("host-sandbox-probe", HostSandboxProbe),
    ):
        write_json(output / f"{name}-v1.schema.json", model.model_json_schema())
    write_json(
        output / "model-history-inspection-v2.schema.json",
        ModelHistoryInspectionV2.model_json_schema(),
    )
    write_json(
        output / "execution-capability-evidence-v2.schema.json",
        ExecutionCapabilityEvidenceV2.model_json_schema(),
    )
    write_json(output / "execution-plan-v2.schema.json", ExecutionPlanV2.model_json_schema())
    print(
        "已更新 Action、Agent、Provider、成本、Smoke、工具、Artifact、Patch、"
        "Process、Context、Coding Eval、可信执行与 OpenAPI Schema"
    )


if __name__ == "__main__":
    main()
