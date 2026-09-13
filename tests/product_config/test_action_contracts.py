from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.domain.models import utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.action_contracts import (
    ProductActionCapabilityEvidence,
    ProductActionCapabilityReport,
    ProductActionConfigV1,
    ProductProcessProfile,
    build_product_action_capability,
    build_product_action_capability_report,
    build_product_action_config,
    build_product_process_profile,
)
from harnessix.product_config.contracts import SecretReference


def process_profile(tmp_path: Path, **changes: object) -> ProductProcessProfile:
    values: dict[str, object] = {
        "profile_id": "unit-tests",
        "version": "2026.09.1",
        "description": "在固定容器内运行单元测试",
        "container_engine": str(tmp_path / "container-engine"),
        "image": "registry.example/harnessix/tests@sha256:" + "a" * 64,
        "program": "/usr/bin/python",
        "arguments": ("-m", "pytest"),
        "selector_policy": "bounded_test_selector",
        "secret_refs": (SecretReference(name="TEST_TOKEN", version="v1"),),
    }
    values.update(changes)
    return build_product_process_profile(**values)  # type: ignore[arg-type]


def test_action_config_and_process_profile_round_trip_strictly(tmp_path: Path) -> None:
    profile = process_profile(tmp_path)
    config = build_product_action_config(process_profiles=(profile,))

    checked_profile = ProductProcessProfile.model_validate_json(profile.model_dump_json())
    checked_config = ProductActionConfigV1.model_validate_json(config.model_dump_json())

    assert checked_profile == profile
    assert checked_config == config
    assert checked_config.process_profiles[0].profile_sha256 == profile.profile_sha256


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("container_engine", "container-engine"),
        ("image", "registry.example/harnessix/tests:latest"),
        ("program", "usr/bin/python"),
        ("network_mode", "restricted"),
        ("arguments", ("-m", "bad\nargument")),
    ],
)
def test_process_profile_rejects_unsafe_execution_boundaries(
    field: str,
    value: object,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        process_profile(tmp_path, **{field: value})


def test_process_profile_rejects_noncanonical_secret_references(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        process_profile(
            tmp_path,
            secret_refs=(
                SecretReference(name="Z_TOKEN", version="v1"),
                SecretReference(name="A_TOKEN", version="v1"),
            ),
        )
    with pytest.raises(ValidationError):
        process_profile(
            tmp_path,
            secret_refs=(
                SecretReference(name="A_TOKEN", version="v1"),
                SecretReference(name="A_TOKEN", version="v1"),
            ),
        )


def test_action_config_rejects_unsorted_duplicate_profiles_and_loose_boolean(
    tmp_path: Path,
) -> None:
    first = process_profile(tmp_path, profile_id="first")
    second = process_profile(tmp_path, profile_id="second")
    with pytest.raises(ValidationError):
        build_product_action_config(process_profiles=(second, first))
    with pytest.raises(ValidationError):
        build_product_action_config(process_profiles=(first, first))
    valid = build_product_action_config()
    with pytest.raises(ValidationError):
        ProductActionConfigV1.model_validate(
            {**valid.model_dump(), "workspace_patch_enabled": "true"}
        )


def test_contract_digests_reject_tampering(tmp_path: Path) -> None:
    profile = process_profile(tmp_path)
    config = build_product_action_config(process_profiles=(profile,))

    with pytest.raises(ValidationError):
        ProductProcessProfile.model_validate(
            {**profile.model_dump(), "description": "被篡改的描述"}
        )
    with pytest.raises(ValidationError):
        ProductActionConfigV1.model_validate(
            {**config.model_dump(), "workspace_patch_enabled": False}
        )


def test_capability_evidence_requires_complete_status_specific_digests() -> None:
    now = utc_now()
    verified = build_product_action_capability(
        capability_id="workspace.patch",
        kind="workspace_patch",
        status="verified",
        reason_code="verified",
        platform="posix",
        binding_digest=canonical_digest("binding"),
        executor_evidence_digest=canonical_digest("executor"),
        probed_at=now,
        ttl_seconds=600,
    )
    omitted = build_product_action_capability(
        capability_id="process.unit-tests",
        kind="process_profile",
        status="omitted",
        reason_code="container_unavailable",
        platform="posix",
        probed_at=now,
    )

    assert verified.expires_at - verified.probed_at == timedelta(minutes=10)
    assert omitted.binding_digest is None
    assert omitted.executor_evidence_digest is None
    with pytest.raises(ValidationError):
        build_product_action_capability(
            capability_id="workspace.patch",
            kind="workspace_patch",
            status="verified",
            reason_code="verified",
            platform="posix",
        )
    with pytest.raises(ValidationError):
        build_product_action_capability(
            capability_id="workspace.patch",
            kind="workspace_patch",
            status="omitted",
            reason_code="disabled",
            platform="posix",
            binding_digest=canonical_digest("binding"),
            executor_evidence_digest=canonical_digest("executor"),
        )
    for invalid_ttl in (0, 601, True):
        with pytest.raises(ValueError):
            build_product_action_capability(
                capability_id="workspace.patch",
                kind="workspace_patch",
                status="omitted",
                reason_code="disabled",
                platform="posix",
                ttl_seconds=invalid_ttl,  # type: ignore[arg-type]
            )


def test_capability_report_is_sorted_unique_and_digest_protected() -> None:
    now = utc_now()
    artifact = build_product_action_capability(
        capability_id="artifact.read",
        kind="artifact",
        status="verified",
        reason_code="verified",
        platform="posix",
        binding_digest=canonical_digest("artifact-binding"),
        executor_evidence_digest=canonical_digest("artifact-executor"),
        probed_at=now,
    )
    patch = build_product_action_capability(
        capability_id="workspace.patch",
        kind="workspace_patch",
        status="omitted",
        reason_code="disabled",
        platform="posix",
        probed_at=now,
    )
    config = build_product_action_config()
    report = build_product_action_capability_report(
        config,
        (artifact, patch),
        created_at=now,
    )

    assert ProductActionCapabilityReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        build_product_action_capability_report(config, (patch, artifact), created_at=now)
    with pytest.raises(ValidationError):
        build_product_action_capability_report(config, (artifact, artifact), created_at=now)
    with pytest.raises(ValidationError):
        build_product_action_capability_report(
            config,
            (artifact,),
            created_at=now - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError):
        build_product_action_capability_report(
            config,
            (artifact,),
            created_at=artifact.expires_at,
        )
    with pytest.raises(ValidationError):
        ProductActionCapabilityEvidence.model_validate(
            {**artifact.model_dump(), "reason_code": "tampered"}
        )
    with pytest.raises(ValidationError):
        ProductActionCapabilityReport.model_validate(
            {**report.model_dump(), "config_sha256": "0" * 64}
        )
