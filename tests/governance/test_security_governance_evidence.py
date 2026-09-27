"""安全治理阶段证据清单完整性；原字节和固定源码输入不可静默漂移。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("directory", "facts_name", "input_paths"),
    [
        (
            "security-governance-2026-09-27-v1",
            "sbom-facts.json",
            {"uv.lock", "pyproject.toml", "governance/sbom.cyclonedx.json"},
        ),
        (
            "secret-scan-2026-09-27-v1",
            "artifact-facts.json",
            {
                "scripts/secret_scan.py",
                "scripts/secret_scan_archives.py",
                "scripts/secret_scan_contracts.py",
                "tests/governance/test_secret_scan.py",
                "Makefile",
                ".github/workflows/ci.yml",
                "uv.lock",
                "pyproject.toml",
            },
        ),
        (
            "license-evidence-2026-09-27-v1",
            "license-facts.json",
            {
                "scripts/license_scan.py",
                "scripts/license_contracts.py",
                "scripts/license_decisions.py",
                "scripts/license_inventory.py",
                "scripts/secret_scan_archives.py",
                "scripts/secret_scan_contracts.py",
                "tests/governance/test_license_archive_evidence.py",
                "tests/governance/test_secret_scan.py",
                "tests/governance/test_supply_chain.py",
                "tests/governance/test_cli_console.py",
                "uv.lock",
                "pyproject.toml",
                "governance/license-policy-v2.json",
                "governance/license-scan-v2.json",
                "governance/license-evidence-v2/index.json",
                "governance/sbom.cyclonedx.json",
                ".gitattributes",
                "Makefile",
                ".github/workflows/ci.yml",
            },
        ),
        (
            "gateway-errors-2026-09-27-v1",
            "gateway-facts.json",
            {
                "src/harnessix/trusted_actions/public_errors.py",
                "src/harnessix/trusted_actions/agent_gateway_support.py",
                "src/harnessix/trusted_actions/agent_gateway_output.py",
                "src/harnessix/trusted_actions/operation_router.py",
                "src/harnessix/trusted_actions/router.py",
                "src/harnessix/trusted_actions/contracts.py",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/trusted_action_session.py",
                "src/harnessix/agent/runtime.py",
                "tests/trusted_actions/test_agent_gateway.py",
                "tests/trusted_actions/test_gateway_error_boundaries.py",
                "tests/trusted_actions/test_gateway_error_runtime.py",
                "tests/trusted_actions/test_operation_error_runtime.py",
                "tests/trusted_actions/test_public_error_leakage.py",
                "tests/trusted_actions/test_plan_error_boundaries.py",
                "docs/baselines/readability-0.9.0-final.json",
            },
        ),
        (
            "returned-failures-2026-09-27-v1",
            "outcome-facts.json",
            {
                "src/harnessix/trusted_actions/router.py",
                "src/harnessix/trusted_actions/public_errors.py",
                "src/harnessix/trusted_actions/contracts.py",
                "src/harnessix/processes/trusted_output.py",
                "spec/public-eval-output-summary-v1.schema.json",
                "docs/baselines/readability-0.9.0-final.json",
                "governance/readability-policy-v1.json",
                "src/harnessix/artifacts/contracts.py",
                "src/harnessix/product_config/eval_action.py",
                "tests/trusted_actions/test_returned_failure_boundaries.py",
                "spec/public-process-stream-summary-v1.schema.json",
                "src/harnessix/trusted_actions/agent_gateway_output.py",
                "tests/trusted_actions/test_process_failure_projection.py",
                "src/harnessix/product_config/process_action.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/trusted_actions/agent_gateway_support.py",
                "tests/trusted_actions/test_schemas.py",
                "src/harnessix/processes/public_output.py",
                "src/harnessix/agent/trusted_action_session.py",
                "scripts/generate_specs.py",
                "src/harnessix/trusted_actions/public_outcomes.py",
                "src/harnessix/trusted_actions/operation_router.py",
                "tests/trusted_actions/test_failure_policy_sources.py",
                "spec/public-process-output-summary-v1.schema.json",
                "tests/trusted_actions/test_returned_failure_runtime.py",
            },
        ),
        (
            "owner-projections-2026-09-27-v1",
            "projection-facts.json",
            {
                "src/harnessix/trusted_actions/output_budget.py",
                "src/harnessix/trusted_actions/agent_gateway_output.py",
                "src/harnessix/trusted_actions/public_outcomes.py",
                "src/harnessix/trusted_actions/public_errors.py",
                "src/harnessix/trusted_actions/operation_router.py",
                "src/harnessix/trusted_actions/contracts.py",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/trusted_action_session.py",
                "src/harnessix/processes/public_output.py",
                "src/harnessix/artifacts/contracts.py",
                "src/harnessix/product_config/process_action.py",
                "src/harnessix/product_config/eval_action.py",
                "src/harnessix/execution/contracts.py",
                "tests/trusted_actions/test_output_budget.py",
                "tests/trusted_actions/test_success_projection_boundaries.py",
                "tests/trusted_actions/test_projection_lifecycle.py",
                "tests/trusted_actions/test_process_success_projection.py",
                "tests/trusted_actions/test_success_projection_runtime.py",
                "tests/trusted_actions/test_process_failure_projection.py",
                "tests/trusted_actions/test_agent_gateway.py",
                "tests/trusted_actions/test_schemas.py",
                "spec/action-output-budget-v1.schema.json",
                "scripts/generate_specs.py",
                "docs/baselines/readability-0.9.0-final.json",
                "governance/readability-policy-v1.json",
            },
        ),
        (
            "executor-output-2026-09-27-v1",
            "budget-facts.json",
            {
                "docs/baselines/readability-0.9.0-final.json",
                "governance/readability-policy-v1.json",
                "scripts/generate_specs.py",
                "spec/action-output-budget-v1.schema.json",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/agent/trusted_action_session.py",
                "src/harnessix/execution/contracts.py",
                "src/harnessix/trusted_actions/agent_gateway_output.py",
                "src/harnessix/trusted_actions/agent_gateway_support.py",
                "src/harnessix/trusted_actions/contracts.py",
                "src/harnessix/trusted_actions/operation_router.py",
                "src/harnessix/trusted_actions/operation_store.py",
                "src/harnessix/trusted_actions/outcome_validation.py",
                "src/harnessix/trusted_actions/output_budget.py",
                "src/harnessix/trusted_actions/public_errors.py",
                "src/harnessix/trusted_actions/public_outcomes.py",
                "src/harnessix/trusted_actions/router.py",
                "tests/trusted_actions/test_executor_output_boundaries.py",
                "tests/trusted_actions/test_executor_output_lifecycle.py",
                "tests/trusted_actions/test_executor_output_runtime.py",
                "tests/trusted_actions/test_outcome_validation.py",
                "tests/trusted_actions/test_router.py",
                "tests/trusted_actions/test_schemas.py",
            },
        ),
        (
            "builtin-success-2026-09-27-v1",
            "contract-facts.json",
            {
                "spec/workspace-patch-output-v1.schema.json",
                "spec/git-push-receipt-v1.schema.json",
                "src/harnessix/mcp/contracts.py",
                "src/harnessix/trusted_actions/output_budget.py",
                "src/harnessix/trusted_actions/router.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/delivery/trusted_action.py",
                "src/harnessix/trusted_actions/public_errors.py",
                "src/harnessix/trusted_actions/contracts.py",
                "scripts/generate_specs.py",
                "tests/trusted_actions/test_process_success_projection.py",
                "src/harnessix/delivery/git_contracts.py",
                "src/harnessix/trusted_actions/public_outcomes.py",
                "src/harnessix/processes/public_output.py",
                "spec/skill-resource-content-v1.schema.json",
                "spec/skill-content-v1.schema.json",
                "docs/baselines/readability-0.9.0-final.json",
                "src/harnessix/delivery/trusted_action_contracts.py",
                "src/harnessix/agent/trusted_action_session.py",
                "src/harnessix/skills/contracts.py",
                "tests/trusted_actions/test_inline_success_lifecycle.py",
                "tests/execution/test_public_tool_contracts.py",
                "spec/mcp-tool-call-output-v1.schema.json",
                "src/harnessix/agent/cancellation.py",
                "tests/trusted_actions/test_builtin_success_runtime.py",
                "src/harnessix/skills/runtime.py",
                "src/harnessix/trusted_actions/builtin_success.py",
                "tests/trusted_actions/test_builtin_success_contracts.py",
                "governance/readability-policy-v1.json",
                "src/harnessix/trusted_actions/agent_gateway_output.py",
                "src/harnessix/mcp/runtime.py",
                "src/harnessix/execution/public_tool_contracts.py",
            },
        ),
        (
            "custom-success-2026-09-27-v1",
            "contract-facts.json",
            {
                "docs/baselines/readability-0.9.0-final.json",
                "governance/readability-policy-v1.json",
                "scripts/generate_specs.py",
                "spec/action-output-budget-v1.schema.json",
                "src/harnessix/agent/approvals.py",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/agent/trusted_action_session.py",
                "src/harnessix/domain/models.py",
                "src/harnessix/domain/public_output_schema.py",
                "src/harnessix/execution/contracts.py",
                "src/harnessix/mcp/schema.py",
                "src/harnessix/mcp/server.py",
                "src/harnessix/trusted_actions/agent_gateway_output.py",
                "src/harnessix/trusted_actions/agent_gateway_support.py",
                "src/harnessix/trusted_actions/contracts.py",
                "src/harnessix/trusted_actions/legacy_projection.py",
                "src/harnessix/trusted_actions/output_budget.py",
                "src/harnessix/trusted_actions/public_outcomes.py",
                "src/harnessix/trusted_actions/router.py",
                "tests/domain/test_public_output_schema.py",
                "tests/mcp/test_server.py",
                "tests/trusted_actions/test_agent_gateway.py",
                "tests/trusted_actions/test_custom_success_authorization.py",
                "tests/trusted_actions/test_custom_success_legacy_binary.py",
                "tests/trusted_actions/test_custom_success_runtime.py",
                "tests/trusted_actions/test_custom_success_schema.py",
                "tests/trusted_actions/test_inline_success_lifecycle.py",
            },
        ),
    ],
)
def test_security_governance_bundle_files_and_source_inputs_match_manifest(
    directory: str, facts_name: str, input_paths: set[str]
) -> None:
    bundle = ROOT / "docs/validation" / directory
    manifest = json.loads((bundle / "bundle-manifest.json").read_bytes())
    entries = manifest["files"]
    names = [entry["path"] for entry in entries]
    assert len(names) == len(set(names)) == 5
    assert set(names) == {
        "README.md",
        "verification.json",
        facts_name,
        "ci-observation.json",
        "review-packet.json",
    }
    for entry in entries:
        path = Path(entry["path"])
        assert not path.is_absolute() and ".." not in path.parts
        body = (bundle / path).read_bytes()
        assert len(body) == entry["size_bytes"]
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
    assert manifest["manifest_self_excluded"] is True
    source_inputs = manifest["source_inputs"]
    assert len(source_inputs) == len(input_paths)
    assert {entry["path"] for entry in source_inputs} == input_paths
    for entry in source_inputs:
        assert entry["code_revision"] == manifest["code_revision"]
        result = subprocess.run(
            ["git", "show", f"{entry['code_revision']}:{entry['path']}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=10,
        )
        assert hashlib.sha256(result.stdout).hexdigest() == entry["sha256"]
    if directory.startswith("secret-scan"):
        facts = json.loads((bundle / facts_name).read_bytes())
        review = json.loads((bundle / "review-packet.json").read_bytes())
        ci = json.loads((bundle / "ci-observation.json").read_bytes())
        assert facts["code_revision"] == review["code_revision"] == manifest["code_revision"]
        assert ci["current_scanner_revision"] == manifest["code_revision"]
        assert review["decision"] == "candidate_only"
        assert ci["prior_success_is_current_scanner_acceptance"] is False
        assert ci["prior_revision"]["conclusion"] == "success"
        assert ci["current_scanner_ci_status"] == "not_started_at_freeze"
        assert facts["reproducible_build_claimed"] is False
        assert {item["kind"] for item in facts["artifacts"]} == {"wheel", "sdist"}
    if directory.startswith("license-evidence"):
        facts = json.loads((bundle / facts_name).read_bytes())
        review = json.loads((bundle / "review-packet.json").read_bytes())
        ci = json.loads((bundle / "ci-observation.json").read_bytes())
        assert facts["code_revision"] == review["code_revision"] == manifest["code_revision"]
        assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
        assert facts["archive_count"] == 777 and facts["unique_blob_count"] == 203
        assert facts["blocked_archive_count"] == 12
        assert facts["independent_cache_reextraction_matches_committed_index"] is True
        assert facts["offline_check_reextracts_archives"] is False
        assert ci["current_ci_status"] == "not_started_at_freeze"
        assert ci["prior_revision"]["conclusion"] == "failure"
        assert sum(item["conclusion"] == "success" for item in ci["prior_revision"]["jobs"]) == 5
        records = facts["blob_records"]
        assert len(records) == len({item["sha256"] for item in records}) == 203
        assert sum(item["size_bytes"] for item in records) == facts["unique_blob_size_bytes"]
        body = (json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        assert hashlib.sha256(body).hexdigest() == facts["blob_records_canonical_sha256"]
    if directory.startswith("gateway-errors"):
        facts = json.loads((bundle / facts_name).read_bytes())
        review = json.loads((bundle / "review-packet.json").read_bytes())
        ci = json.loads((bundle / "ci-observation.json").read_bytes())
        verification = json.loads((bundle / "verification.json").read_bytes())
        assert facts["code_revision"] == review["code_revision"] == manifest["code_revision"]
        assert (
            verification["code_revision"]
            == ci["current_code_revision"]
            == manifest["code_revision"]
        )
        assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
        assert ci["current_ci_status"] == "not_started_at_freeze"
        assert ci["previous_revision"]["conclusion"] == "failure"
        assert ci["previous_windows_success_is_new_gateway_acceptance"] is False
        assert facts["phase_registry"]["context"] == {}
        assert facts["schema_migration"] is False and facts["action_fingerprint_changed"] is False
        assert facts["new_test_counts"]["total"] == 57
        assert verification["full_regression"]["passed"] == 4113
        assert verification["full_regression"]["skipped"] == 32
        assert verification["full_regression"]["untracked_attack_draft_excluded"] is True
        gap = facts["structured_outcome_open_gap"]
        assert gap["status"] == "confirmed_open_gap" and gap["acceptance_pass_claimed"] is False
        assert gap["provider_requests"] == 2 and gap["real_model_requests"] == 0
        assert gap["surfaces"]["protocol"]["unregistered_code_present"] is True
        assert gap["surfaces"]["telemetry"]["diagnostic_payload_present"] is False


def test_returned_failure_bundle_keeps_scope_and_release_blockers_explicit() -> None:
    bundle = ROOT / "docs/validation/returned-failures-2026-09-27-v1"
    facts = json.loads((bundle / "outcome-facts.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    verification = json.loads((bundle / "verification.json").read_bytes())
    assert (
        facts["code_revision"]
        == review["code_revision"]
        == ci["current_code_revision"]
        == verification["code_revision"]
    )
    assert facts["matrix"]["new_tests"] == 106 and facts["matrix"]["special_total"] == 107
    assert all(facts["state_invariants"].values())
    assert facts["public_projection_does_not_clean_legacy_private_storage"] is True
    assert facts["clean_build"]["reproducible_build_claimed"] is False
    assert len(facts["clean_build"]["artifact_records"]) == 2
    assert all(
        not item["contains_untracked_security_draft"]
        for item in facts["clean_build"]["artifact_records"]
    )
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert review["current_ci_accepted"] is False
    assert (
        verification["external_model_requests"] == 0 and verification["make_check_passed"] is False
    )
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["previous_success_is_current_failure_contract_acceptance"] is False
    assert (
        ci["previous_revision"]["status"] == "completed"
        and ci["previous_revision"]["conclusion"] == "failure"
    )
    assert sum(item["conclusion"] == "success" for item in ci["previous_revision"]["jobs"]) == 4
    assert ci["previous_python_failure"]["blocked_archive_count"] == 12


def test_owner_projection_evidence_preserves_unclosed_scope_and_effect_facts():
    bundle = ROOT / "docs/validation/owner-projections-2026-09-27-v1"
    facts = json.loads((bundle / "projection-facts.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    assert facts["code_revision"] == review["code_revision"] == ci["code_revision"]
    assert facts["executor_calls_per_scenario"] == 1
    assert facts["reconciliation_calls_per_scenario"] == 0
    assert facts["runtime_projection_calls_before_saved_result"] == 2
    assert facts["gateway_single_attempt_projection_calls"] == 1
    assert facts["audit_success_preserved"] is True
    assert facts["raw_executor_output_budget_closed"] is False
    assert facts["non_cooperative_provider_hard_kill_claimed"] is False
    assert facts["reproducible_build_claimed"] is False
    assert facts["untracked_security_draft_excluded"] is True
    assert all(not item["untracked_security_draft_present"] for item in facts["artifacts"])
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_acceptance"] is False
    assert ci["prior_revision"]["conclusion"] == "failure"
    assert sum(item["conclusion"] == "success" for item in ci["prior_revision"]["jobs"]) == 4


def test_executor_output_evidence_distinguishes_declaration_and_confirmed_fact():
    bundle = ROOT / "docs/validation/executor-output-2026-09-27-v1"
    facts = json.loads((bundle / "budget-facts.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    assert facts["code_revision"] == review["code_revision"] == ci["code_revision"]
    assert facts["public_failure_policy_version"] == "harnessix.public-action-failure/v2"
    assert facts["new_finite_error_codes"] == 9
    assert (
        facts["raw_envelope_budget_closed"] is True and facts["post_return_deadline_closed"] is True
    )
    assert facts["confirmed_audit_fact_rewritten"] is False
    assert facts["invalid_return_is_not_confirmed_effect"] is True
    assert facts["write_rejection_is_unknown"] is True
    assert facts["real_file_append_count"] == 1 and facts["hard_exit_code"] == 73
    assert facts["hard_exit_after_budget_rejection_before_complete"] is True
    assert facts["hard_exit_recovery_reexecutions"] == 0
    assert facts["hard_exit_recovery_operation_states"] == ["interrupted", "completed"]
    assert facts["scripted_runtime_public_surfaces_nonempty"] is True
    assert facts["valid_small_success_json_public_authorization_closed"] is False
    assert facts["noncooperative_hard_kill_claimed"] is False
    assert facts["reproducible_build_claimed"] is False
    assert all(not item["untracked_security_draft_present"] for item in facts["artifacts"])
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_acceptance"] is False


def test_builtin_success_evidence_does_not_waive_custom_or_release_boundaries():
    bundle = ROOT / "docs/validation/builtin-success-2026-09-27-v1"
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    assert facts["code_revision"] == review["code_revision"] == ci["code_revision"]
    assert facts["special_tests"] == 167 and facts["new_special_tests"] == 119
    assert facts["prior_owner_tests"] == 48
    assert facts["builtin_formal_contracts_checked"] is True
    assert facts["inline_and_owner_checked"] is True
    assert facts["raw_summary_checked_before_owner_publication"] is True
    assert facts["owner_exports_are_same_class"] is True and facts["existing_schema_drift"] is False
    assert facts["new_package_dependencies"] == facts["new_dependency_cycles"] == []
    assert facts["confirmed_audit_success_preserved"] is True
    assert facts["inline_fault_has_failed_turn_and_known_success_effect"] is True
    assert facts["reexecutions_after_projection_failure"] == 0
    assert facts["custom_public_authorization_closed"] is False
    assert facts["secret_end_to_end_public_authorization_closed"] is False
    assert facts["custom_gap_observation"]["observed_open_gap"] is True
    assert facts["custom_gap_observation"]["acceptance_pass_claimed"] is False
    assert facts["schema_migration"] is False and facts["hard_preemption_claimed"] is False
    assert facts["readability_policy_relaxed"] is False
    assert all(not item["untracked_security_draft_present"] for item in facts["artifacts"])
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_acceptance"] is False


def test_custom_success_contract_evidence_does_not_waive_value_or_release_boundaries():
    bundle = ROOT / "docs/validation/custom-success-2026-09-27-v1"
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    assert facts["code_revision"] == review["code_revision"] == ci["code_revision"]
    assert facts["special_tests"] == 91 and facts["new_special_tests"] == 89
    assert facts["prior_mcp_tests"] == 2
    assert facts["custom_success_field_authorization_closed_for_current_gateway_and_mcp"] is True
    assert facts["missing_contract_defaults_to_deny"] is True
    assert facts["extra_fields_not_dropped"] is True
    assert facts["full_descriptor_fingerprint_checked"] is True
    assert facts["builtin_contract_priority_preserved"] is True
    assert facts["raw_summary_checked_before_owner_publication"] is True
    assert facts["recovery_summary_contract_and_hash_checked"] is True
    assert facts["confirmed_audit_success_preserved"] is True
    assert facts["reexecutions_after_projection_failure"] == 0
    assert facts["actual_runtime_public_surfaces_nonempty"] is True
    assert facts["actual_low_risk_mcp_client_tested"] is True
    assert facts["independent_old_binary_sqlite_plan_approval_audit_unchanged"] is True
    assert facts["old_descriptor_json_and_fingerprint_unchanged"] is True
    assert facts["terminal_contract_drift_has_no_body_artifact_execution_or_reconciliation"] is True
    assert facts["nonterminal_contract_drift_denied"] is True
    assert facts["secret_end_to_end_public_authorization_closed"] is False
    assert facts["persisted_old_session_public_body_retroactively_cleaned"] is False
    assert facts["owner_internal_budget_closed"] is False
    assert facts["existing_generated_schema_drift"] is False
    assert facts["binding_route_or_audit_schema_migration"] is False
    assert facts["new_package_dependencies"] == facts["new_dependency_cycles"] == []
    assert facts["readability_policy_relaxed"] is False
    assert facts["hard_preemption_claimed"] is False
    assert facts["reproducible_build_claimed"] is False
    assert facts["remote_mcp_oauth_egress_claimed"] is False
    assert all(not item["untracked_security_draft_present"] for item in facts["artifacts"])
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_acceptance"] is False
