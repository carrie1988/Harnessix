from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from harnessix.agent.models import AgentEvent, Thread
from harnessix.api import create_app
from harnessix.artifacts.contracts import (
    ArtifactPage,
    ArtifactPolicy,
    ArtifactRef,
    ReadArtifactInput,
)
from harnessix.domain.models import ActionRequest
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ProviderEvent
from harnessix.models.costs import CostReport
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
from harnessix.processes.contracts import (
    ProcessLimits,
    ProcessRequest,
    ProcessResult,
    ProcessStream,
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


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    output = Path("spec")
    output.mkdir(exist_ok=True)
    write_json(output / "action-contract-v1.schema.json", ActionRequest.model_json_schema())
    write_json(output / "openapi.json", create_app().openapi())
    write_json(output / "agent-event-v8.schema.json", AgentEvent.model_json_schema())
    write_json(output / "agent-thread-v8.schema.json", Thread.model_json_schema())
    write_json(output / "provider-event-v3.schema.json", TypeAdapter(ProviderEvent).json_schema())
    write_json(output / "openai-chat-config-v1.schema.json", OpenAIChatConfig.model_json_schema())
    write_json(output / "anthropic-config-v1.schema.json", AnthropicConfig.model_json_schema())
    write_json(output / "price-snapshot-v1.schema.json", PriceSnapshot.model_json_schema())
    write_json(output / "cost-report-v1.schema.json", CostReport.model_json_schema())
    write_json(output / "model-smoke-config-v1.schema.json", SmokeConfig.model_json_schema())
    write_json(output / "model-smoke-report-v1.schema.json", SmokeReport.model_json_schema())
    write_json(
        output / "batch-diff-record-v1.schema.json", TypeAdapter(BatchDiffRecord).json_schema()
    )
    for name, model in (
        ("process-request", ProcessRequest),
        ("process-limits", ProcessLimits),
        ("process-stream", ProcessStream),
        ("process-result", ProcessResult),
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
    ):
        write_json(output / f"{name}-v1.schema.json", model.model_json_schema())
    print(
        "已更新 Action、Agent、Provider、成本、Smoke、工具、Artifact、Patch、"
        "Process 与 OpenAPI Schema"
    )


if __name__ == "__main__":
    main()
