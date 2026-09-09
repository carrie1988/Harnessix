from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from harnessix.agent.models import (
    AgentEvent,
    CompactionWindow,
    Thread,
    ThreadArchiveRecord,
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
from harnessix.delivery.contracts import (
    WorkspaceDiffDocument,
    WorkspaceDiffEntry,
    WorkspaceFileVersion,
    WorkspaceMutation,
    WorkspaceTransactionPlan,
    WorkspaceTransactionRecord,
)
from harnessix.delivery.git_contracts import (
    GitCheckpoint,
    GitCommitRecord,
    GitCommitSpec,
    GitPushActionInput,
    GitPushIntent,
    GitPushReceipt,
    GitRepositoryBinding,
    ManagedGitWorktreeBinding,
    ManagedGitWorktreePlan,
    ManagedGitWorktreeRecord,
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
from harnessix.hooks.contracts import (
    HookActionInput,
    HookActionOutput,
    HookDefinition,
    HookDispatch,
    HookDispatchResult,
    HookMatcher,
    HookRegistrySnapshot,
    HookRunEvent,
    HookRunPlan,
    HookRunSnapshot,
    HookTrustGrant,
)
from harnessix.mcp.contracts import (
    McpCatalogSnapshot,
    McpConnectionEvent,
    McpConnectionSnapshot,
    McpServerIdentity,
    McpToolCallOutput,
    McpToolSnapshot,
)
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ProviderEvent
from harnessix.models.costs import CostReport, CostReportV2, CostReportV3
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
from harnessix.product_config.contracts import (
    ConfigAuditEvent,
    ConfigMigrationReceipt,
    ConfigurationDiagnosticReport,
    ProductConfigSnapshot,
    ProductConfigV1,
    ProductConfigV2,
    ProfileSelection,
    ProviderFallbackDecision,
)
from harnessix.protocol.contracts import (
    AgentCommandParams,
    AgentQueryParams,
    EventsNextResult,
    EventsReplayResult,
    InitializeParams,
    InitializeResult,
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    PublicEvent,
    PublicItem,
    ThreadView,
    TurnView,
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
from harnessix.skills.contracts import (
    SkillAccessEvent,
    SkillCatalogSnapshot,
    SkillContent,
    SkillDiscoveryIssue,
    SkillLoadInput,
    SkillManifestSnapshot,
    SkillNameConflict,
    SkillResourceContent,
    SkillResourceReadInput,
    SkillSourceSnapshot,
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
from harnessix.trusted_actions.contracts import (
    ActionAuditEvent,
    ActionExecutionOutcome,
    ActionRoutePlan,
    ActionRouteSnapshot,
    CanonicalActionResource,
    CodingActionInvocation,
    TrustedToolBinding,
)
from harnessix.workspace.contracts import WorkspaceLease, WorkspaceSnapshot


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    output = Path("spec")
    output.mkdir(exist_ok=True)
    write_json(output / "action-contract-v1.schema.json", ActionRequest.model_json_schema())
    write_json(output / "openapi.json", create_app().openapi())
    write_json(output / "agent-event-v19.schema.json", AgentEvent.model_json_schema())
    write_json(output / "agent-thread-v19.schema.json", Thread.model_json_schema())
    write_json(
        output / "context-inspection-v3.schema.json", ContextInspectionV3.model_json_schema()
    )
    write_json(output / "provider-event-v3.schema.json", TypeAdapter(ProviderEvent).json_schema())
    write_json(output / "openai-chat-config-v1.schema.json", OpenAIChatConfig.model_json_schema())
    write_json(output / "anthropic-config-v1.schema.json", AnthropicConfig.model_json_schema())
    write_json(output / "product-config-v1.schema.json", ProductConfigV1.model_json_schema())
    write_json(output / "product-config-v2.schema.json", ProductConfigV2.model_json_schema())
    write_json(output / "price-snapshot-v1.schema.json", PriceSnapshot.model_json_schema())
    write_json(output / "cost-report-v1.schema.json", CostReport.model_json_schema())
    write_json(output / "cost-report-v2.schema.json", CostReportV2.model_json_schema())
    write_json(output / "cost-report-v3.schema.json", CostReportV3.model_json_schema())
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
        ("workspace-file-version", WorkspaceFileVersion),
        ("workspace-mutation", WorkspaceMutation),
        ("workspace-transaction-plan", WorkspaceTransactionPlan),
        ("workspace-transaction-record", WorkspaceTransactionRecord),
        ("workspace-diff-entry", WorkspaceDiffEntry),
        ("workspace-diff", WorkspaceDiffDocument),
        ("git-repository-binding", GitRepositoryBinding),
        ("managed-git-worktree-plan", ManagedGitWorktreePlan),
        ("managed-git-worktree-binding", ManagedGitWorktreeBinding),
        ("managed-git-worktree-record", ManagedGitWorktreeRecord),
        ("git-checkpoint", GitCheckpoint),
        ("git-commit-spec", GitCommitSpec),
        ("git-commit-record", GitCommitRecord),
        ("git-push-intent", GitPushIntent),
        ("git-push-action-input", GitPushActionInput),
        ("git-push-receipt", GitPushReceipt),
        ("action-resource", CanonicalActionResource),
        ("trusted-tool-binding", TrustedToolBinding),
        ("coding-action-invocation", CodingActionInvocation),
        ("action-route-plan", ActionRoutePlan),
        ("action-execution-outcome", ActionExecutionOutcome),
        ("action-audit-event", ActionAuditEvent),
        ("action-route-snapshot", ActionRouteSnapshot),
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
        ("mcp-server-identity", McpServerIdentity),
        ("mcp-tool-snapshot", McpToolSnapshot),
        ("mcp-catalog-snapshot", McpCatalogSnapshot),
        ("mcp-connection-event", McpConnectionEvent),
        ("mcp-connection-snapshot", McpConnectionSnapshot),
        ("mcp-tool-call-output", McpToolCallOutput),
        ("skill-source-snapshot", SkillSourceSnapshot),
        ("skill-manifest-snapshot", SkillManifestSnapshot),
        ("skill-discovery-issue", SkillDiscoveryIssue),
        ("skill-name-conflict", SkillNameConflict),
        ("skill-catalog-snapshot", SkillCatalogSnapshot),
        ("skill-load-input", SkillLoadInput),
        ("skill-resource-read-input", SkillResourceReadInput),
        ("skill-content", SkillContent),
        ("skill-resource-content", SkillResourceContent),
        ("skill-access-event", SkillAccessEvent),
        ("hook-matcher", HookMatcher),
        ("hook-definition", HookDefinition),
        ("hook-trust-grant", HookTrustGrant),
        ("hook-registry-snapshot", HookRegistrySnapshot),
        ("hook-dispatch", HookDispatch),
        ("hook-action-input", HookActionInput),
        ("hook-action-output", HookActionOutput),
        ("hook-run-plan", HookRunPlan),
        ("hook-run-event", HookRunEvent),
        ("hook-run-snapshot", HookRunSnapshot),
        ("hook-dispatch-result", HookDispatchResult),
        ("product-config-snapshot", ProductConfigSnapshot),
        ("profile-selection", ProfileSelection),
        ("configuration-diagnostic", ConfigurationDiagnosticReport),
        ("config-migration-receipt", ConfigMigrationReceipt),
        ("config-audit-event", ConfigAuditEvent),
        ("provider-fallback-decision", ProviderFallbackDecision),
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
    for name, model in (
        ("agent-protocol-jsonrpc-request", JsonRpcRequest),
        ("agent-protocol-jsonrpc-notification", JsonRpcNotification),
        ("agent-protocol-jsonrpc-success", JsonRpcSuccessResponse),
        ("agent-protocol-jsonrpc-error", JsonRpcErrorResponse),
        ("agent-protocol-initialize-params", InitializeParams),
        ("agent-protocol-initialize-result", InitializeResult),
        ("agent-protocol-thread", ThreadView),
        ("agent-protocol-turn", TurnView),
        ("agent-protocol-item", PublicItem),
        ("agent-protocol-event", PublicEvent),
        ("agent-protocol-replay-result", EventsReplayResult),
        ("agent-protocol-next-result", EventsNextResult),
    ):
        write_json(output / f"{name}-v1.schema.json", model.model_json_schema())
    write_json(
        output / "agent-protocol-command-params-v1.schema.json",
        TypeAdapter(AgentCommandParams).json_schema(),
    )
    write_json(
        output / "agent-protocol-query-params-v1.schema.json",
        TypeAdapter(AgentQueryParams).json_schema(),
    )
    print(
        "已更新 Action、Agent、Provider、成本、Smoke、工具、Artifact、Patch、"
        "Process、Context、Coding Eval、可信执行、MCP、Skill、Hook、"
        "Provider产品配置、Agent Protocol 与 OpenAPI Schema"
    )


if __name__ == "__main__":
    main()
