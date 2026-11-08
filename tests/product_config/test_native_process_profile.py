"""外部边界原生进程Profile的合同、能力证明与诊断（ADR 0121）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.supervisor_capabilities import probe_posix_process_capability
from harnessix.product_config.action_contracts import (
    ProductActionConfigV1,
    ProductNativeProcessProfile,
    build_product_action_config,
    build_product_native_process_profile,
    build_product_process_profile,
)
from harnessix.product_config.action_diagnostics import diagnose_product_actions
from harnessix.product_config.native_process import (
    EXTERNAL_BOUNDARY_ENVIRONMENT,
    attest_native_process_profile,
    native_process_binding,
    native_process_descriptor,
    probe_native_process_profile,
)
from tests.product_config.test_process_action import _NoSecrets


def native_profile(**overrides: Any) -> ProductNativeProcessProfile:
    values: dict[str, Any] = {
        "profile_id": "task-shell",
        "version": "2026.11.1",
        "description": "评测任务容器内的Shell",
        "backend": "external_boundary",
        "program": "/bin/bash",
        "arguments": ("-c",),
        "environment": (("PATH", "/opt/env/bin:/usr/bin:/bin"), ("LANG", "C.UTF-8")),
    }
    return build_product_native_process_profile(**(values | overrides))


def test_empty_native_list_keeps_existing_config_digest_and_wire_shape() -> None:
    config = build_product_action_config()
    assert "native_process_profiles" not in config.model_dump(mode="json")
    assert (
        config.config_sha256
        == build_product_action_config(native_process_profiles=()).config_sha256
    )
    with_native = build_product_action_config(native_process_profiles=(native_profile(),))
    assert ProductActionConfigV1.model_validate_json(with_native.model_dump_json()) == with_native
    assert with_native.config_sha256 != config.config_sha256


def test_profile_digest_binds_environment_and_sorts_presets() -> None:
    profile = native_profile()
    assert profile.environment == (("LANG", "C.UTF-8"), ("PATH", "/opt/env/bin:/usr/bin:/bin"))
    assert profile.workspace_mode == "read_write"
    changed = native_profile(environment=(("PATH", "/usr/bin:/bin"),))
    assert changed.profile_sha256 != profile.profile_sha256
    with pytest.raises(ValidationError):
        ProductNativeProcessProfile.model_validate(
            profile.model_dump(mode="json") | {"timeout_seconds": 301}
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"program": "/usr/bin/python3"},
        {"arguments": ()},
        {"arguments": ("-c", "echo fixed")},
        {"program": "bin/bash"},
        {"backend": "landlock"},
        {"environment": (("HTTPS_PROXY", "http://proxy"),)},
        {"environment": (("HARNESSIX_EXTERNAL_BOUNDARY", "1"),)},
        {"description": "two\nlines"},
        {"timeout_seconds": 0},
    ],
)
def test_profile_rejects_unbounded_or_unfrozen_execution(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        native_profile(**overrides)


def test_sandbox_backends_accept_sandbox_fields_without_changing_old_digests() -> None:
    boundary = native_profile()
    assert set(boundary.model_dump(mode="json")) >= {"workspace_mode"}
    assert "write_roots" not in boundary.model_dump(mode="json")
    assert "allow_workspace_env_files" not in boundary.model_dump(mode="json")
    sandboxed = native_profile(
        backend="bubblewrap",
        workspace_mode="read_only",
        write_roots=("/srv/cache/b", "/srv/cache/a"),
        allow_workspace_env_files=True,
        environment=(("LANG", "C.UTF-8"),),
    )
    assert sandboxed.write_roots == ("/srv/cache/a", "/srv/cache/b")
    assert ProductNativeProcessProfile.model_validate_json(sandboxed.model_dump_json()) == sandboxed
    assert native_profile(backend="seatbelt").backend == "seatbelt"
    assert native_profile(backend="host").workspace_mode == "read_write"


@pytest.mark.parametrize(
    "overrides",
    [
        {"backend": "external_boundary", "workspace_mode": "read_only"},
        {"backend": "host", "write_roots": ("/srv/cache",)},
        {"backend": "external_boundary", "allow_workspace_env_files": True},
        {"backend": "seatbelt", "write_roots": ("/",)},
        {"backend": "seatbelt", "write_roots": ("relative/path",)},
        {"backend": "seatbelt", "write_roots": ("/srv/../etc",)},
        {"backend": "seatbelt", "environment": (("HOME", "/tmp/h"),)},
        {"backend": "bubblewrap", "environment": (("TMPDIR", "/tmp"),)},
        {"backend": "host", "environment": (("XDG_CACHE_HOME", "/tmp"),)},
    ],
)
def test_sandbox_only_fields_and_managed_variables_are_guarded(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        native_profile(**overrides)


@pytest.mark.skipif(os.name != "posix", reason="原生后端只支持POSIX Process Owner")
def test_other_backends_are_never_attested_as_external_boundary() -> None:
    capability = probe_posix_process_capability()
    for backend in ("seatbelt", "bubblewrap"):
        assert attest_native_process_profile(
            native_profile(backend=backend),
            capability,
            environ={EXTERNAL_BOUNDARY_ENVIRONMENT: "1"},
        ) == ("native_sandbox_site_unavailable", None)
    # 沙箱外命令不需要边界断言，但如实记为host提供方，不冒充外部边界。
    reason, attested = attest_native_process_profile(
        native_profile(backend="host"), capability, environ={EXTERNAL_BOUNDARY_ENVIRONMENT: "1"}
    )
    assert reason == "verified" and attested is not None
    assert attested.capabilities.provider == "host"


def test_native_and_container_ids_share_one_namespace(tmp_path: Path) -> None:
    container = build_product_process_profile(
        profile_id="task-shell",
        version="1",
        description="容器Shell",
        container_engine=str(tmp_path / "docker"),
        image="sha256:" + "a" * 64,
        program="/bin/sh",
        arguments=("-c",),
        selector_policy="shell_command",
    )
    with pytest.raises(ValueError):
        build_product_action_config(
            process_profiles=(container,), native_process_profiles=(native_profile(),)
        )
    with pytest.raises(ValueError):
        build_product_action_config(
            native_process_profiles=(native_profile(profile_id="z"), native_profile(profile_id="a"))
        )


def test_descriptor_uses_command_namespace_and_states_boundary_honestly() -> None:
    profile = native_profile()
    descriptor = native_process_descriptor(profile)
    binding = native_process_binding(profile)
    assert descriptor.name == "exec_command.task-shell"
    assert descriptor.version == f"harnessix.native-command/v1:{profile.profile_sha256[:24]}"
    assert binding.executor_id == "product.command-profile.task-shell"
    assert descriptor.requires_approval and descriptor.requires_idempotency
    assert descriptor.supports_reconciliation and not descriptor.supports_parallel_calls
    assert "改动会保留" in descriptor.description
    assert "Harnessix不另加沙箱" in descriptor.description
    assert set(descriptor.input_schema["required"]) == {"profile", "cmd"}


@pytest.mark.skipif(os.name != "posix", reason="外部边界只支持POSIX Process Owner")
def test_attestation_requires_explicit_boundary_assertion() -> None:
    capability = probe_posix_process_capability()
    profile = native_profile()
    for environ in (
        {},
        {EXTERNAL_BOUNDARY_ENVIRONMENT: "0"},
        {EXTERNAL_BOUNDARY_ENVIRONMENT: "true"},
    ):
        assert attest_native_process_profile(profile, capability, environ=environ) == (
            "external_boundary_unasserted",
            None,
        )
    assert attest_native_process_profile(
        profile, None, environ={EXTERNAL_BOUNDARY_ENVIRONMENT: "1"}
    ) == ("process_owner_unavailable", None)

    reason, attested = attest_native_process_profile(
        profile, capability, environ={EXTERNAL_BOUNDARY_ENVIRONMENT: "1"}
    )
    assert reason == "verified" and attested is not None
    # 如实记录：Harnessix未加隔离，网络未由Harnessix限制。
    assert (attested.sandbox.level, attested.sandbox.backend, attested.sandbox.network) == (
        "host_guarded",
        "host",
        "full",
    )
    assert attested.capabilities.provider == "external-boundary"
    assert attested.capabilities.sandbox_levels == ("host_guarded",)
    assert dict(attested.environment) == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PATH": "/opt/env/bin:/usr/bin:/bin",
    }


@pytest.mark.skipif(os.name != "posix", reason="外部边界只支持POSIX Process Owner")
async def test_probe_binds_owner_and_matches_stateless_attestation(tmp_path: Path) -> None:
    profile = native_profile()
    environ = {EXTERNAL_BOUNDARY_ENVIRONMENT: "1"}
    assert probe_native_process_profile(profile, object(), environ=environ).reason_code == (
        "process_owner_unavailable"
    )
    async with PosixProcessSupervisor(tmp_path / "owner") as supervisor:
        probe = probe_native_process_profile(profile, supervisor, environ=environ)
        assert probe.reason_code == "verified" and probe.verified is not None
        assert probe.verified.supervisor is supervisor
        _, attested = attest_native_process_profile(profile, supervisor.capability, environ=environ)
        assert attested is not None
        assert probe.verified.executor_evidence_sha256 == attested.executor_evidence_sha256
        unasserted = probe_native_process_profile(profile, supervisor, environ={})
        assert (unasserted.reason_code, unasserted.verified) == (
            "external_boundary_unasserted",
            None,
        )


@pytest.mark.skipif(os.name != "posix", reason="外部边界只支持POSIX Process Owner")
@pytest.mark.parametrize("asserted", [False, True])
def test_diagnostics_report_native_profile_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, asserted: bool
) -> None:
    if asserted:
        monkeypatch.setenv(EXTERNAL_BOUNDARY_ENVIRONMENT, "1")
    else:
        monkeypatch.delenv(EXTERNAL_BOUNDARY_ENVIRONMENT, raising=False)
    profile = native_profile()
    report = diagnose_product_actions(
        build_product_action_config(native_process_profiles=(profile,)),
        platform="posix",
        secrets=_NoSecrets(),
        workspace=tmp_path,
    )
    native = next(
        item for item in report.capabilities if item.capability_id == "exec_command.task-shell"
    )
    if asserted:
        assert (native.status, native.reason_code) == ("verified", "verified")
        assert native.binding_digest == native_process_binding(profile).binding_digest
    else:
        assert (native.status, native.reason_code) == ("omitted", "external_boundary_unasserted")
        assert native.binding_digest is None
