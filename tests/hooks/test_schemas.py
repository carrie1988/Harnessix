from __future__ import annotations

import json
from pathlib import Path

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


def test_committed_hook_schemas_match_runtime_contracts() -> None:
    root = Path(__file__).parents[2] / "spec"
    expected = {
        "hook-matcher-v1.schema.json": HookMatcher.model_json_schema(),
        "hook-definition-v1.schema.json": HookDefinition.model_json_schema(),
        "hook-trust-grant-v1.schema.json": HookTrustGrant.model_json_schema(),
        "hook-registry-snapshot-v1.schema.json": HookRegistrySnapshot.model_json_schema(),
        "hook-dispatch-v1.schema.json": HookDispatch.model_json_schema(),
        "hook-action-input-v1.schema.json": HookActionInput.model_json_schema(),
        "hook-action-output-v1.schema.json": HookActionOutput.model_json_schema(),
        "hook-run-plan-v1.schema.json": HookRunPlan.model_json_schema(),
        "hook-run-event-v1.schema.json": HookRunEvent.model_json_schema(),
        "hook-run-snapshot-v1.schema.json": HookRunSnapshot.model_json_schema(),
        "hook-dispatch-result-v1.schema.json": HookDispatchResult.model_json_schema(),
    }
    for name, schema in expected.items():
        assert json.loads((root / name).read_text(encoding="utf-8")) == schema
