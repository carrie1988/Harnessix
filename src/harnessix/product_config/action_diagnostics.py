"""Product Action无状态能力诊断：复用正式Binding与执行证明但不创建运行状态。"""

from __future__ import annotations

from harnessix.agent.errors import KernelError
from harnessix.delivery.trusted_action import (
    WORKSPACE_PATCH_TOOL,
    workspace_patch_binding,
    workspace_patch_executor_evidence,
    workspace_patch_supported,
)
from harnessix.processes.supervisor_capabilities import (
    probe_posix_process_capability,
    probe_windows_process_capability,
)
from harnessix.product_config.action_contracts import (
    ProductActionCapabilityEvidence,
    ProductActionCapabilityReport,
    ProductActionConfigV1,
    build_product_action_capability,
    build_product_action_capability_report,
)
from harnessix.product_config.process_action import product_process_binding
from harnessix.product_config.process_profile import (
    probe_product_process_profile_attestation,
    process_tool_name,
)
from harnessix.secrets.provider import SecretProvider
from harnessix.workspace.contracts import PlatformKind


def diagnose_product_actions(
    config: ProductActionConfigV1,
    *,
    platform: PlatformKind,
    secrets: SecretProvider,
) -> ProductActionCapabilityReport:
    """生成Doctor可公开的同源能力报告；Process探测只读且不创建Lease Store。"""

    evidence: list[ProductActionCapabilityEvidence] = [_diagnose_patch(config, platform)]
    owner_capability = None
    if config.process_profiles:
        try:
            owner_capability = (
                probe_windows_process_capability()
                if platform == "windows"
                else probe_posix_process_capability()
            )
        except KernelError:
            owner_capability = None
    for profile in config.process_profiles:
        tool = process_tool_name(profile.profile_id)
        if owner_capability is None:
            evidence.append(
                build_product_action_capability(
                    capability_id=tool,
                    kind="process_profile",
                    status="omitted",
                    reason_code="process_owner_unavailable",
                    platform=platform,
                )
            )
            continue
        result = probe_product_process_profile_attestation(
            profile,
            owner_capability,
            secrets,
        )
        if result.attested is None:
            evidence.append(
                build_product_action_capability(
                    capability_id=tool,
                    kind="process_profile",
                    status="omitted",
                    reason_code=result.reason_code,
                    platform=platform,
                )
            )
            continue
        binding = product_process_binding(profile)
        evidence.append(
            build_product_action_capability(
                capability_id=tool,
                kind="process_profile",
                status="verified",
                reason_code="verified",
                platform=platform,
                binding_digest=binding.binding_digest,
                executor_evidence_digest=result.attested.executor_evidence_sha256,
            )
        )
    return build_product_action_capability_report(
        config,
        tuple(sorted(evidence, key=lambda item: item.capability_id)),
    )


def _diagnose_patch(
    config: ProductActionConfigV1,
    platform: PlatformKind,
) -> ProductActionCapabilityEvidence:
    reason = (
        "disabled"
        if not config.workspace_patch_enabled
        else "verified"
        if platform == "posix" and workspace_patch_supported()
        else "platform_not_supported"
    )
    if reason != "verified":
        return build_product_action_capability(
            capability_id=WORKSPACE_PATCH_TOOL,
            kind="workspace_patch",
            status="omitted",
            reason_code=reason,
            platform=platform,
        )
    binding = workspace_patch_binding()
    return build_product_action_capability(
        capability_id=WORKSPACE_PATCH_TOOL,
        kind="workspace_patch",
        status="verified",
        reason_code="verified",
        platform=platform,
        binding_digest=binding.binding_digest,
        executor_evidence_digest=workspace_patch_executor_evidence(),
    )
