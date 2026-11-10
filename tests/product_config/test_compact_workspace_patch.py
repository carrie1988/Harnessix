"""紧凑Patch合同：只缩短模型可见描述/Schema，解码、审批、事务与效果与标准合同一致。"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import get_args

import pytest
from pydantic import ValidationError

from harnessix.agent.models import ToolCallContent, ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action import (
    WORKSPACE_PATCH_TOOL,
    WORKSPACE_PATCH_VERSION,
    workspace_patch_binding,
    workspace_patch_descriptor,
)
from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput
from harnessix.delivery.workspace_patch_compact import (
    WORKSPACE_PATCH_COMPACT_VERSION,
    compact_workspace_patch_schema,
    decode_compact_workspace_patch,
)
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _approval


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


OLD = "def value():\n    return 1\n"
EDIT = {
    "files": [
        {
            "operation": "edit",
            "path": "src/modified.py",
            "expected_sha256": sha(OLD),
            "edits": [{"old_text": "return 1", "new_text": "return 2"}],
        }
    ]
}
SAMPLES = [
    EDIT,
    {"files": [{"operation": "create", "path": "a.py", "content": "x\n", "mode": 420}]},
    {
        "files": [
            {
                "operation": "replace",
                "path": "a.py",
                "expected_sha256": "0" * 64,
                "content": "",
                "mode": 493,
            }
        ]
    },
    {"files": [{"operation": "delete", "path": "a.py", "expected_sha256": "0" * 64}]},
    # 下列非法组合在紧凑Schema形状上可表达，但必须照旧被同一严格模型拒绝。
    {"files": [{**EDIT["files"][0], "content": "x"}]},
    {"files": [{"operation": "replace", "path": "a.py", "content": "x", "mode": 420}]},
    {"files": [{"operation": "create", "path": "a.py", "content": "x", "mode": 511}]},
    {"files": [{"operation": "edit", "path": "a.py", "expected_sha256": "zz", "edits": []}]},
    {"files": [], "spec_version": "harnessix.workspace-patch-input/v2"},
    {"files": [{**EDIT["files"][0], "unknown": 1}]},
]


def standard_decode(arguments):
    return WorkspacePatchInput.model_validate_json(json.dumps(arguments, ensure_ascii=False))


@pytest.mark.parametrize("arguments", SAMPLES)
def test_compact_decoder_accepts_and_rejects_exactly_like_standard_contract(arguments):
    try:
        expected = standard_decode(arguments)
    except ValidationError:
        with pytest.raises(ValidationError):
            decode_compact_workspace_patch(arguments)
    else:
        assert decode_compact_workspace_patch(arguments) == expected


def test_compact_schema_only_names_fields_and_values_of_the_strict_model():
    schema = compact_workspace_patch_schema()
    item = schema["properties"]["files"]["items"]
    assert set(item["properties"]) <= set(WorkspacePatchFile.model_fields)
    assert set(item["required"]) <= set(item["properties"])
    operation = WorkspacePatchFile.model_fields["operation"].annotation
    assert set(item["properties"]["operation"]["enum"]) == set(get_args(operation))
    assert item["properties"]["mode"]["enum"] == [420, 493]
    assert schema["properties"]["files"]["maxItems"] == 16


def test_compact_descriptor_is_a_distinct_versioned_binding_and_standard_is_unchanged():
    standard, compact = workspace_patch_descriptor(), workspace_patch_descriptor("compact")
    assert standard.version == WORKSPACE_PATCH_VERSION
    assert standard.input_schema == WorkspacePatchInput.model_json_schema()
    assert compact.name == standard.name == WORKSPACE_PATCH_TOOL
    assert compact.version == WORKSPACE_PATCH_COMPACT_VERSION
    assert compact.input_schema == compact_workspace_patch_schema()
    assert "\n" not in compact.description and len(compact.description) <= 1000
    for field in ("effect_class", "risk_level", "requires_approval", "requires_idempotency"):
        assert getattr(compact, field) == getattr(standard, field)
    assert workspace_patch_binding("compact").binding_digest != (
        workspace_patch_binding().binding_digest
    )
    assert workspace_patch_binding("compact").input_schema_sha256 == canonical_digest(
        compact_workspace_patch_schema()
    )


@asynccontextmanager
async def compact_product(tmp_path):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src/modified.py").write_bytes(OLD.encode())
    environment = build_fixed_product_action_environment(root)
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "state/delivery")
    leases = WorkspaceLeaseStore(tmp_path / "state/leases.db")
    sessions = SQLiteSessionStore(tmp_path / "state/sessions.db")
    artifacts = SQLiteArtifactStore(sessions)
    router = TrustedActionRouter(
        plans=plans, audit=audit, workspace_root=environment.workspace_root
    )
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        composition = build_product_action_composition(
            build_product_action_config(),
            environment,
            router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=tools.workspace_scope,
            patch_contract="compact",
        )
        provider = ScriptedProvider([])
        try:
            async with AgentRuntime(
                sessions,
                provider,
                trusted_actions=composition.gateway,
                artifacts=artifacts,
                scoped_tools=tools,
            ) as runtime:
                yield root, runtime, provider, composition
        finally:
            if composition.gateway is not None:
                composition.gateway.close()
            plans.close()
            audit.close()
            transactions.close()
            leases.close()


async def run_patch(runtime, provider, root, arguments):
    proposal = WorkspacePatchInput.model_construct(files=())
    step = _action_step(proposal)
    step[1] = step[1].model_copy(update={"arguments": arguments})
    provider.steps = (step, answer("已处理"))
    thread = await runtime.create_thread(str(root))
    return thread, await runtime.run_turn(thread.thread_id, "修改文件", request_id="patch")


async def test_gateway_advertises_compact_descriptor_and_applies_after_approval(tmp_path):
    async with compact_product(tmp_path) as (root, runtime, provider, composition):
        (advertised,) = [
            d for d in composition.gateway.definitions() if d.name == WORKSPACE_PATCH_TOOL
        ]
        assert advertised == workspace_patch_descriptor("compact")
        thread, waiting = await run_patch(runtime, provider, root, EDIT)
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        call = next(i.content for i in waiting.items if isinstance(i.content, ToolCallContent))
        assert call.tool_version == WORKSPACE_PATCH_COMPACT_VERSION
        approval = _approval(waiting)
        await runtime.reply_approval(
            thread.thread_id,
            waiting.turn_id,
            approval.approval_id,
            fingerprint=approval.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="user"),
        )
        completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
        assert completed.status is TurnStatus.COMPLETED
        assert (root / "src/modified.py").read_text() == OLD.replace("return 1", "return 2")


async def test_invalid_combination_fails_without_approval_or_effect(tmp_path):
    async with compact_product(tmp_path) as (root, runtime, provider, _):
        invalid = {"files": [{**EDIT["files"][0], "content": "x"}]}
        _, turn = await run_patch(runtime, provider, root, invalid)
        results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
        assert turn.status is TurnStatus.COMPLETED
        assert results[0].outcome == "failed"
        assert results[0].error.code == "tool_invalid_arguments"
        assert (root / "src/modified.py").read_text() == OLD


async def test_create_under_a_missing_parent_is_a_correctable_failure(tmp_path):
    async with compact_product(tmp_path) as (root, runtime, provider, _):
        missing = {
            "files": [
                {"operation": "create", "path": "src/new_dir/a.py", "content": "x\n", "mode": 420}
            ]
        }
        _, turn = await run_patch(runtime, provider, root, missing)
        results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
        assert turn.status is TurnStatus.COMPLETED
        assert results[0].outcome == "failed"
        assert results[0].error.code == "workspace_parent_missing"
        assert not (root / "src/new_dir").exists()
