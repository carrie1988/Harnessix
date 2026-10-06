from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from harnessix.agent.models import (
    AgentEvent,
    CompactionWindow,
    Thread,
    ThreadArchiveRecord,
)
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
from harnessix.delivery.rollback_action import WorkspaceRollbackInput
from harnessix.delivery.trusted_action_contracts import (
    PublicWorkspacePatchOutput,
    WorkspaceActionReviewRecord,
    WorkspacePatchInput,
)
from harnessix.delivery.workspace_record_contracts import WorkspaceStoredRecord
from harnessix.delivery.workspace_record_v3_contracts import WorkspaceStoredRecordV3
from harnessix.delivery.workspace_v2_contracts import (
    WorkspaceTransactionPlanV2,
    WorkspaceTransactionRecordV2,
)
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
from harnessix.evals.provider_suite_contracts import (
    CodingEvalProviderSuiteEvidenceManifest,
    CodingEvalProviderSuiteRunConfig,
    CodingEvalProviderSuiteRunReport,
)
from harnessix.evals.suite_contracts import (
    CodingEvalSuitePlan,
    CodingEvalSuiteReport,
    CodingEvalTranscriptEvidence,
)
from harnessix.evals.suite_execution_contracts import (
    CodingEvalSuiteCaseRunResult,
    CodingEvalSuiteExecutionState,
    CodingEvalSuiteRunConfig,
    CodingEvalSuiteRunReport,
)
from harnessix.evals.task_pack_contracts import (
    CodingEvalReviewOracle,
    CodingEvalTaskPack,
    CodingEvalTaskPackMaterialization,
)
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionCapabilityEvidence,
    ExecutionCapabilityEvidenceV2,
    ExecutionIntent,
    ExecutionPlan,
    ExecutionPlanV2,
)
from harnessix.execution.versioned_contracts import ExecutionPlanV3
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
from harnessix.processes.owner_protocol import (
    ProcessOwnerCommand,
    ProcessOwnerStart,
    ProcessOwnerStartV2,
)
from harnessix.processes.owner_receipt import ProcessOwnerReceipt, ProcessOwnerReceiptV2
from harnessix.processes.public_output import (
    PublicEvalOutputSummary,
    PublicProcessOutputSummary,
    PublicProcessStreamSummary,
)
from harnessix.processes.supervision_contracts import (
    ProcessCapabilityProbe,
    ProcessLaunchBinding,
    ProcessLease,
    ProcessOutputObservation,
    ProcessSpec,
)
from harnessix.product_config.action_contracts import (
    ProductActionCapabilityEvidence,
    ProductActionCapabilityReport,
    ProductActionConfigAuditEvent,
    ProductActionConfigSnapshot,
    ProductActionConfigV1,
    ProductActionStartupRecoveryReport,
    ProductProcessProfile,
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
from harnessix.product_config.git_baseline_contracts import ProductGitDeliveryBaseline
from harnessix.product_config.git_delivery_observed_contracts import (
    ProductGitDeliveryCoreV2,
    ProductGitDeliveryPlanV2,
)
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitCheckpointInput,
    ProductGitCommitInput,
    ProductGitDeliveryCore,
    ProductGitDeliveryPlan,
)
from harnessix.product_config.git_delivery_review_contracts import (
    ProductGitActionReviewDocument,
    ProductGitActionReviewSummary,
)
from harnessix.product_config.git_parent_contracts import (
    ProductGitDeliveryBaselineV2,
    ProductGitDeliverySourceV2,
)
from harnessix.product_config.git_prefix_catalog import GitPrefixCatalog
from harnessix.product_config.git_user_observation_contracts import ProductGitUserObservation
from harnessix.product_config.product_contracts import (
    ConfigurationDraft,
    ConfigurationWriteReceipt,
    ProductPreflightReport,
)
from harnessix.product_config.workspace_patch_source_contracts import ProductGitDeliverySource
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
from harnessix.tools.contracts import (
    ListFilesInput,
    ListFilesOutput,
    ReadFileInput,
    ReadFileOutput,
    ReadFileSnapshotOutput,
)
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
from harnessix.trusted_actions.output_budget import ActionOutputBudget
from harnessix.trusted_actions.recovery_contracts import (
    ActionRecoveryScanReport,
    ActionRouteOperation,
)
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2, ActionRouteSnapshotV2
from harnessix.workspace.contracts import WorkspaceLease, WorkspaceSnapshot
from harnessix.workspace.parent_closure_contracts import (
    WorkspaceParentClosureManifest,
    WorkspaceParentObservationChunk,
)
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2

if __package__ or __spec__ is not None:
    from scripts.cli_console import configure_utf8_console
else:
    from cli_console import configure_utf8_console


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def generate_specs(output: Path) -> None:
    """把当前Python合同确定性导出到指定目录。"""

    output.mkdir(parents=True, exist_ok=True)
    for name, model in (
        ("product-git-checkpoint-input-v1", ProductGitCheckpointInput),
        ("product-git-commit-input-v1", ProductGitCommitInput),
        ("product-git-delivery-core-v1", ProductGitDeliveryCore),
        ("product-git-delivery-core-v2", ProductGitDeliveryCoreV2),
        ("product-git-delivery-plan-v2", ProductGitDeliveryPlanV2),
        ("product-git-delivery-plan-v1", ProductGitDeliveryPlan),
        ("product-git-user-observation-v1", ProductGitUserObservation),
        ("product-git-action-review-summary-v1", ProductGitActionReviewSummary),
        ("product-git-action-review-document-v1", ProductGitActionReviewDocument),
    ):
        write_json(output / f"{name}.schema.json", model.model_json_schema())
    write_json(output / "agent-event-v20.schema.json", AgentEvent.model_json_schema())
    write_json(output / "agent-thread-v20.schema.json", Thread.model_json_schema())
    write_json(
        output / "context-inspection-v3.schema.json", ContextInspectionV3.model_json_schema()
    )
    write_json(output / "provider-event-v3.schema.json", TypeAdapter(ProviderEvent).json_schema())
    write_json(output / "openai-chat-config-v1.schema.json", OpenAIChatConfig.model_json_schema())
    write_json(output / "anthropic-config-v1.schema.json", AnthropicConfig.model_json_schema())
    write_json(
        output / "product-git-delivery-source-v1.schema.json",
        ProductGitDeliverySource.model_json_schema(),
    )
    write_json(
        output / "product-git-baseline-v1.schema.json",
        ProductGitDeliveryBaseline.model_json_schema(),
    )
    write_json(output / "product-config-v1.schema.json", ProductConfigV1.model_json_schema())
    write_json(output / "product-config-v2.schema.json", ProductConfigV2.model_json_schema())
    write_json(
        output / "product-action-config-v1.schema.json",
        ProductActionConfigV1.model_json_schema(),
    )
    write_json(
        output / "product-action-capability-v1.schema.json",
        ProductActionCapabilityEvidence.model_json_schema(),
    )
    write_json(
        output / "product-action-capability-report-v1.schema.json",
        ProductActionCapabilityReport.model_json_schema(),
    )
    write_json(
        output / "product-action-config-snapshot-v1.schema.json",
        ProductActionConfigSnapshot.model_json_schema(),
    )
    write_json(
        output / "product-action-config-audit-event-v1.schema.json",
        ProductActionConfigAuditEvent.model_json_schema(),
    )
    write_json(
        output / "product-action-startup-recovery-v1.schema.json",
        ProductActionStartupRecoveryReport.model_json_schema(),
    )
    write_json(
        output / "product-process-profile-v1.schema.json",
        ProductProcessProfile.model_json_schema(),
    )
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
    write_json(
        output / "workspace-patch-rollback-input-v1.schema.json",
        WorkspaceRollbackInput.model_json_schema(),
    )
    write_json(
        output / "workspace-patch-input-v1.schema.json",
        WorkspacePatchInput.model_json_schema(),
    )
    write_json(
        output / "workspace-patch-output-v1.schema.json",
        PublicWorkspacePatchOutput.model_json_schema(),
    )
    write_json(
        output / "workspace-action-review-record-v1.schema.json",
        TypeAdapter(WorkspaceActionReviewRecord).json_schema(),
    )
    for name, model in (
        ("git-prefix-catalog", GitPrefixCatalog),
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
        ("action-output-budget", ActionOutputBudget),
        ("public-process-stream-summary", PublicProcessStreamSummary),
        ("public-process-output-summary", PublicProcessOutputSummary),
        ("public-eval-output-summary", PublicEvalOutputSummary),
        ("process-owner-start", ProcessOwnerStart),
        ("process-owner-command", ProcessOwnerCommand),
        ("process-owner-receipt", ProcessOwnerReceipt),
        ("list-files-input", ListFilesInput),
        ("list-files-output", ListFilesOutput),
        ("read-file-input", ReadFileInput),
        ("read-file-output", ReadFileOutput),
        ("read-file-snapshot-output", ReadFileSnapshotOutput),
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
        ("coding-eval-suite-plan", CodingEvalSuitePlan),
        ("coding-eval-suite-report", CodingEvalSuiteReport),
        ("coding-eval-transcript-evidence", CodingEvalTranscriptEvidence),
        ("coding-eval-suite-run-config", CodingEvalSuiteRunConfig),
        ("coding-eval-suite-case-run-result", CodingEvalSuiteCaseRunResult),
        ("coding-eval-suite-execution-state", CodingEvalSuiteExecutionState),
        ("coding-eval-suite-run-report", CodingEvalSuiteRunReport),
        ("coding-eval-provider-suite-run-config", CodingEvalProviderSuiteRunConfig),
        ("coding-eval-provider-suite-run-report", CodingEvalProviderSuiteRunReport),
        ("coding-eval-provider-suite-evidence", CodingEvalProviderSuiteEvidenceManifest),
        ("coding-eval-task-pack", CodingEvalTaskPack),
        ("coding-eval-task-pack-materialization", CodingEvalTaskPackMaterialization),
        ("coding-eval-review-oracle", CodingEvalReviewOracle),
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
        ("action-route-operation", ActionRouteOperation),
        ("action-recovery-scan", ActionRecoveryScanReport),
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
        ("configuration-draft", ConfigurationDraft),
        ("configuration-diagnostic", ConfigurationDiagnosticReport),
        ("configuration-write-receipt", ConfigurationWriteReceipt),
        ("product-preflight", ProductPreflightReport),
        ("config-migration-receipt", ConfigMigrationReceipt),
        ("config-audit-event", ConfigAuditEvent),
        ("provider-fallback-decision", ProviderFallbackDecision),
    ):
        write_json(output / f"{name}-v1.schema.json", model.model_json_schema())
    write_json(
        output / "process-owner-start-v2.schema.json", ProcessOwnerStartV2.model_json_schema()
    )
    write_json(
        output / "process-owner-receipt-v2.schema.json", ProcessOwnerReceiptV2.model_json_schema()
    )
    write_json(
        output / "model-history-inspection-v2.schema.json",
        ModelHistoryInspectionV2.model_json_schema(),
    )
    write_json(
        output / "execution-capability-evidence-v2.schema.json",
        ExecutionCapabilityEvidenceV2.model_json_schema(),
    )
    write_json(output / "execution-plan-v2.schema.json", ExecutionPlanV2.model_json_schema())
    write_json(
        output / "workspace-stored-record-v2.schema.json", WorkspaceStoredRecord.model_json_schema()
    )
    write_json(
        output / "workspace-snapshot-v2.schema.json", WorkspaceSnapshotV2.model_json_schema()
    )
    write_json(
        output / "workspace-parent-closure-v1.schema.json",
        WorkspaceParentClosureManifest.model_json_schema(),
    )
    write_json(
        output / "workspace-parent-observations-v1.schema.json",
        WorkspaceParentObservationChunk.model_json_schema(),
    )
    for name, model in (
        ("product-git-delivery-source-v2", ProductGitDeliverySourceV2),
        ("product-git-baseline-v2", ProductGitDeliveryBaselineV2),
        ("workspace-transaction-plan-v2", WorkspaceTransactionPlanV2),
        ("workspace-transaction-record-v2", WorkspaceTransactionRecordV2),
        ("workspace-stored-record-v3", WorkspaceStoredRecordV3),
        ("execution-plan-v3", ExecutionPlanV3),
        ("action-route-plan-v2", ActionRoutePlanV2),
        ("action-route-snapshot-v2", ActionRouteSnapshotV2),
    ):
        write_json(output / f"{name}.schema.json", model.model_json_schema())
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


def check_specs(expected: Path) -> list[str]:
    """校验当前生成集合；额外文件是仍受支持的历史合同，不按陈旧文件删除。"""

    if not expected.is_dir():
        return [f"合同目录不存在：{expected.as_posix()}"]
    if any(path.is_symlink() for path in expected.rglob("*")):
        return ["合同目录不能包含符号链接"]

    with tempfile.TemporaryDirectory(prefix="harnessix-spec-check-") as directory:
        generated = Path(directory)
        generate_specs(generated)
        expected_files = {
            path.relative_to(expected).as_posix(): path
            for path in expected.rglob("*")
            if path.is_file()
        }
        generated_files = {
            path.relative_to(generated).as_posix(): path
            for path in generated.rglob("*")
            if path.is_file()
        }
        findings = [
            f"已提交合同缺少生成文件：{path}"
            for path in sorted(generated_files.keys() - expected_files.keys())
        ]
        findings.extend(
            f"已提交合同内容漂移：{path}"
            for path in sorted(expected_files.keys() & generated_files.keys())
            if expected_files[path].read_bytes() != generated_files[path].read_bytes()
        )
        return findings


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成或验证Harnessix公共Schema")
    parser.add_argument("--output", type=Path, default=Path("spec"))
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_console()
    args = _argument_parser().parse_args(argv)
    if args.check:
        findings = check_specs(args.output)
        if findings:
            print("\n".join(findings))
            return 1
        print("合同一致性检查通过")
        return 0

    generate_specs(args.output)
    print(
        "已更新 Action、Agent、Provider、成本、Smoke、工具、Artifact、Patch、"
        "Process、Context、Coding Eval、可信执行、MCP、Skill、Hook、"
        "Provider产品配置、Agent Protocol 与 OpenAPI Schema"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
