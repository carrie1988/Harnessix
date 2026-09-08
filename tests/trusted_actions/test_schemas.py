from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from harnessix.delivery.git_contracts import GitPushActionInput, GitPushIntent, GitPushReceipt
from harnessix.trusted_actions.contracts import (
    ActionAuditEvent,
    ActionExecutionOutcome,
    ActionRoutePlan,
    ActionRouteSnapshot,
    CanonicalActionResource,
    CodingActionInvocation,
    TrustedToolBinding,
)


def test_action_plane_public_schemas_match_generated_contracts() -> None:
    contracts: dict[str, type[BaseModel]] = {
        "action-resource": CanonicalActionResource,
        "trusted-tool-binding": TrustedToolBinding,
        "coding-action-invocation": CodingActionInvocation,
        "action-route-plan": ActionRoutePlan,
        "action-execution-outcome": ActionExecutionOutcome,
        "action-audit-event": ActionAuditEvent,
        "action-route-snapshot": ActionRouteSnapshot,
        "git-push-intent": GitPushIntent,
        "git-push-action-input": GitPushActionInput,
        "git-push-receipt": GitPushReceipt,
    }
    root = Path(__file__).parents[2] / "spec"
    for name, model in contracts.items():
        persisted = json.loads((root / f"{name}-v1.schema.json").read_text(encoding="utf-8"))
        assert persisted == model.model_json_schema()
