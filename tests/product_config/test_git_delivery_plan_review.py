"""审查复现的精确回归；合同拒绝不代表已形成业务批准或产品执行接线。"""

from datetime import timedelta, timezone

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_contracts import GitCommitSpec
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitCommitInput,
    ProductGitDeliveryCore,
    ProductGitDeliveryPlan,
)
from tests.product_config.git_delivery_plan_support import canonical_bytes, make_case, rehash


@pytest.fixture
def case(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield make_case(GitMaterialCAS(store), tmp_path, action="commit")


def test_added_resource_is_not_part_of_approved_core(case):
    payload = case.plan.model_dump(mode="json")
    route = payload["route"]
    route["resources"].append(
        {
            **route["resources"][0],
            "identifier_sha256": "f" * 64,
        }
    )
    route["resources"].sort(
        key=lambda row: (
            row["kind"],
            row["access"],
            row["identifier_sha256"],
            row["attributes_sha256"],
        )
    )
    route["resources_sha256"] = canonical_digest(
        [
            [row[k] for k in ("kind", "access", "identifier_sha256", "attributes_sha256")]
            for row in route["resources"]
        ]
    )
    rehash(route)
    rehash(payload)
    with pytest.raises((ValidationError, KernelError)):
        ProductGitDeliveryPlan.model_validate_json(canonical_bytes(payload))


def test_same_instant_different_timezone_is_different_commit_encoding(case):
    payload = case.core.model_dump(mode="json")
    spec_time = case.core.commit_spec.authored_at
    changed = spec_time.astimezone(timezone(timedelta(hours=8)))
    assert spec_time == changed and spec_time.isoformat() != changed.isoformat()
    payload["call"]["arguments"]["authored_at"] = changed.isoformat()
    rehash(payload)
    with pytest.raises((ValidationError, KernelError)):
        ProductGitDeliveryCore.model_validate_json(canonical_bytes(payload))


def test_checkpoint_net_mutation_digest_cannot_be_rehashed_away(case):
    payload = case.core.model_dump(mode="json")
    spec = payload["commit_spec"]
    spec["checkpoint"]["mutations_digest"] = "f" * 64
    rehash(spec["checkpoint"], "digest")
    rehash(spec)
    rehash(payload)
    with pytest.raises((ValidationError, KernelError)):
        ProductGitDeliveryCore.model_validate_json(canonical_bytes(payload))


@pytest.mark.parametrize("field", ["author_name", "author_email"])
@pytest.mark.parametrize("kind", ["input", "original_spec"])
def test_nul_author_is_rejected_before_original_object_parsing(case, field, kind):
    payload = (
        case.core.call.arguments.copy()
        if kind == "input"
        else case.core.commit_spec.model_dump(mode="json")
    )
    payload[field] = "a\0b" if field == "author_name" else "a\0@b"
    if kind != "input":
        rehash(payload)
    model = ProductGitCommitInput if kind == "input" else GitCommitSpec
    with pytest.raises((ValidationError, KernelError)):
        model.model_validate_json(canonical_bytes(payload))


@pytest.mark.parametrize("model", [ProductGitDeliveryCore, ProductGitDeliveryPlan])
def test_scope_schema_matches_new_hex_wire_and_complete_fields(model):
    definitions = model.model_json_schema()["$defs"]
    for name in (
        "GitInventoryScope",
        "GitInventoryRoots",
        "GitInventoryObject",
        "GitTreeEntry",
        "GitObjectRead",
        "GitCommitReferences",
        "GitTreeClosureLimits",
        "GitInventoryMetrics",
        "GitBaseHistoryBoundary",
        "GitObjectMaterialReference",
    ):
        assert definitions[name]["additionalProperties"] is False
        assert set(definitions[name]["required"]) == set(definitions[name]["properties"]) or (
            name == "GitObjectMaterialReference"
        )
    assert definitions["GitTreeEntry"]["properties"]["name"]["pattern"] == r"^(?:[0-9a-f]{2})*$"
