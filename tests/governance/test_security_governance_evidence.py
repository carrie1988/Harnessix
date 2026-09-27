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
        (
            "secret-publication-2026-09-27-v1",
            "contract-facts.json",
            {
                "docs/baselines/readability-0.9.0-final.json",
                "governance/readability-policy-v1.json",
                "scripts/generate_specs.py",
                "spec/action-output-budget-v1.schema.json",
                "src/harnessix/secrets/provider.py",
                "src/harnessix/secrets/redaction.py",
                "src/harnessix/secrets/publication.py",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/agent/trusted_action_session.py",
                "src/harnessix/execution/contracts.py",
                "src/harnessix/trusted_actions/contracts.py",
                "src/harnessix/trusted_actions/router.py",
                "src/harnessix/trusted_actions/agent_gateway.py",
                "src/harnessix/trusted_actions/agent_gateway_output.py",
                "src/harnessix/trusted_actions/agent_gateway_support.py",
                "src/harnessix/trusted_actions/output_budget.py",
                "src/harnessix/trusted_actions/public_errors.py",
                "src/harnessix/trusted_actions/public_outcomes.py",
                "src/harnessix/product_config/action_composition.py",
                "src/harnessix/product_config/action_runtime.py",
                "src/harnessix/product_config/process_action.py",
                "src/harnessix/mcp/server.py",
                "tests/secrets/test_publication.py",
                "tests/trusted_actions/test_secret_publication.py",
                "tests/trusted_actions/test_output_budget.py",
                "tests/trusted_actions/test_secret_publication_runtime.py",
                "tests/product_config/test_secret_scope_composition.py",
                "tests/mcp/test_server.py",
            },
        ),
        (
            "product-publication-2026-09-27-v1",
            "contract-facts.json",
            {
                "tests/agent/test_store.py",
                "tests/agent/test_store_maintenance.py",
                "tests/agent/test_wal_initialization.py",
                "docs/baselines/readability-0.9.0-final.json",
                "governance/readability-policy-v1.json",
                "scripts/generate_specs.py",
                "spec/artifact-ref-v1.schema.json",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/publication.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/agent/runtime_configuration.py",
                "src/harnessix/agent/runtime_recovery.py",
                "src/harnessix/artifacts/action_output_store.py",
                "src/harnessix/artifacts/action_review_store.py",
                "src/harnessix/artifacts/batch_diff.py",
                "src/harnessix/artifacts/batch_verify.py",
                "src/harnessix/artifacts/contracts.py",
                "src/harnessix/artifacts/persistence.py",
                "src/harnessix/artifacts/ports.py",
                "src/harnessix/artifacts/publication.py",
                "src/harnessix/artifacts/sqlite.py",
                "src/harnessix/product_config/runtime.py",
                "src/harnessix/product_config/server.py",
                "src/harnessix/secrets/provider.py",
                "src/harnessix/secrets/publication.py",
                "src/harnessix/secrets/redaction.py",
                "src/harnessix/session/migrations/0028_artifact_publication_proof.sql",
                "src/harnessix/trusted_actions/agent_gateway.py",
                "src/harnessix/trusted_actions/agent_gateway_output.py",
                "src/harnessix/trusted_actions/output_budget.py",
                "tests/agent/test_batch_session_upgrade.py",
                "tests/agent/test_process_session_upgrade.py",
                "tests/agent/test_publication.py",
                "tests/agent/test_publication_runtime.py",
                "tests/agent/test_session_upgrade.py",
                "tests/artifacts/test_batch_diff_upgrade.py",
                "tests/artifacts/test_process_output_upgrade.py",
                "tests/artifacts/test_publication_persistence.py",
                "tests/artifacts/test_publication_upgrade.py",
                "tests/governance/test_security_governance_evidence.py",
                "tests/product_config/test_publication_scope.py",
            },
        ),
        (
            "typed-binary-publication-2026-09-28-v1",
            "contract-facts.json",
            {
                "docs/baselines/readability-0.9.0-final.json",
                "governance/readability-policy-v1.json",
                "scripts/generate_specs.py",
                "spec/process-lease-v1.schema.json",
                "spec/process-owner-start-v1.schema.json",
                "spec/process-owner-start-v2.schema.json",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/publication.py",
                "src/harnessix/agent/runtime_recovery.py",
                "src/harnessix/agent/trusted_action_session.py",
                "src/harnessix/artifacts/action_output_store.py",
                "src/harnessix/artifacts/batch_verify.py",
                "src/harnessix/artifacts/binary_projection.py",
                "src/harnessix/artifacts/contracts.py",
                "src/harnessix/artifacts/persistence.py",
                "src/harnessix/artifacts/publication.py",
                "src/harnessix/artifacts/sqlite.py",
                "src/harnessix/processes/output_artifact.py",
                "src/harnessix/processes/owner_output.py",
                "src/harnessix/processes/owner_protocol.py",
                "src/harnessix/processes/owner_receipt.py",
                "src/harnessix/processes/posix_owner.py",
                "src/harnessix/processes/supervision_planner.py",
                "src/harnessix/processes/supervisor.py",
                "src/harnessix/processes/trusted_output.py",
                "src/harnessix/processes/windows_owner.py",
                "src/harnessix/product_config/action_runtime.py",
                "src/harnessix/product_config/process_action.py",
                "src/harnessix/product_config/server.py",
                "src/harnessix/secrets/provider.py",
                "src/harnessix/secrets/publication.py",
                "src/harnessix/secrets/redaction.py",
                "src/harnessix/session/migrations/0028_artifact_publication_proof.sql",
                "src/harnessix/trusted_actions/agent_gateway_output.py",
                "src/harnessix/trusted_actions/agent_gateway_support.py",
                "src/harnessix/trusted_actions/public_errors.py",
                "tests/artifacts/test_binary_publication.py",
                "tests/artifacts/test_publication_persistence.py",
                "tests/governance/test_security_governance_evidence.py",
                "tests/processes/test_output_protection.py",
                "tests/product_config/test_process_action.py",
                "tests/product_config/test_publication_scope.py",
                "tests/trusted_actions/test_publication_recovery.py",
            },
        ),
        (
            "model-text-publication-2026-09-28-v1",
            "contract-facts.json",
            {
                ".github/workflows/ci.yml",
                "docs/baselines/readability-0.9.0-final.json",
                "governance/documentation-policy-v1.json",
                "governance/readability-policy-v1.json",
                "pyproject.toml",
                "scripts/documentation_check.py",
                "scripts/generate_specs.py",
                "scripts/license_scan.py",
                "scripts/readability_report.py",
                "scripts/sbom_generate.py",
                "scripts/secret_scan.py",
                "src/harnessix/agent/attempt_accounting.py",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/model_text.py",
                "src/harnessix/agent/publication.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/app_server/service.py",
                "src/harnessix/models/contracts.py",
                "src/harnessix/product_config/runtime.py",
                "src/harnessix/product_config/server.py",
                "src/harnessix/sdk/agent_client.py",
                "src/harnessix/secrets/provider.py",
                "src/harnessix/secrets/publication.py",
                "src/harnessix/secrets/redaction.py",
                "src/harnessix/secrets/text_publication.py",
                "src/harnessix/session/sqlite.py",
                "tests/agent/test_model_publication_runtime.py",
                "tests/agent/test_text_publication.py",
                "tests/artifacts/test_binary_publication.py",
                "tests/governance/test_security_governance_evidence.py",
                "tests/product_config/test_publication_scope.py",
                "uv.lock",
            },
        ),
        (
            "input-persistence-2026-09-28-v1",
            "contract-facts.json",
            {
                ".github/workflows/ci.yml",
                "docs/baselines/readability-0.9.0-final.json",
                "governance/documentation-policy-v1.json",
                "governance/readability-policy-v1.json",
                "pyproject.toml",
                "scripts/documentation_check.py",
                "scripts/generate_specs.py",
                "scripts/license_scan.py",
                "scripts/readability_report.py",
                "scripts/sbom_generate.py",
                "scripts/secret_scan.py",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/errors.py",
                "src/harnessix/agent/input_publication.py",
                "src/harnessix/agent/lifecycle.py",
                "src/harnessix/agent/models.py",
                "src/harnessix/agent/publication.py",
                "src/harnessix/agent/question_events.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/app_server/command_runtime.py",
                "src/harnessix/app_server/server.py",
                "src/harnessix/app_server/service.py",
                "src/harnessix/product_config/runtime.py",
                "src/harnessix/product_config/server.py",
                "src/harnessix/protocol/contracts.py",
                "src/harnessix/protocol/projection.py",
                "src/harnessix/protocol/requests.py",
                "src/harnessix/sdk/agent_client.py",
                "src/harnessix/secrets/provider.py",
                "src/harnessix/secrets/publication.py",
                "src/harnessix/secrets/redaction.py",
                "src/harnessix/session/sqlite.py",
                "tests/agent/test_input_publication_runtime.py",
                "tests/app_server/test_command_publication.py",
                "tests/governance/test_security_governance_evidence.py",
                "uv.lock",
            },
        ),
        (
            "protocol-frame-publication-2026-09-28-v1",
            "contract-facts.json",
            {
                ".github/workflows/ci.yml",
                "docs/baselines/readability-0.9.0-final.json",
                "governance/documentation-policy-v1.json",
                "governance/readability-policy-v1.json",
                "pyproject.toml",
                "uv.lock",
                "scripts/documentation_check.py",
                "scripts/generate_specs.py",
                "scripts/license_scan.py",
                "scripts/readability_report.py",
                "scripts/sbom_generate.py",
                "scripts/secret_scan.py",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/errors.py",
                "src/harnessix/agent/input_publication.py",
                "src/harnessix/agent/publication.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/app_server/frame_publication.py",
                "src/harnessix/app_server/handshake.py",
                "src/harnessix/app_server/server.py",
                "src/harnessix/app_server/command_runtime.py",
                "src/harnessix/app_server/service.py",
                "src/harnessix/app_server/stdio.py",
                "src/harnessix/product_config/runtime.py",
                "src/harnessix/product_config/server.py",
                "src/harnessix/protocol/contracts.py",
                "src/harnessix/protocol/codec.py",
                "src/harnessix/sdk/agent_client.py",
                "src/harnessix/secrets/provider.py",
                "src/harnessix/secrets/publication.py",
                "src/harnessix/secrets/redaction.py",
                "src/harnessix/session/sqlite.py",
                "tests/app_server/test_frame_publication.py",
                "tests/app_server/test_command_publication.py",
                "tests/product_config/test_protocol_publication_cli.py",
                "tests/agent/test_input_publication_runtime.py",
                "tests/governance/test_security_governance_evidence.py",
            },
        ),
        (
            "event-seal-core-2026-09-28-v1",
            "contract-facts.json",
            {
                ".github/workflows/ci.yml",
                "docs/baselines/readability-0.9.0-final.json",
                "governance/documentation-policy-v1.json",
                "governance/readability-policy-v1.json",
                "pyproject.toml",
                "uv.lock",
                "scripts/documentation_check.py",
                "scripts/generate_specs.py",
                "scripts/license_scan.py",
                "scripts/readability_report.py",
                "scripts/sbom_generate.py",
                "scripts/secret_scan.py",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/errors.py",
                "src/harnessix/agent/models.py",
                "src/harnessix/agent/publication.py",
                "src/harnessix/secrets/publication.py",
                "src/harnessix/secrets/provider.py",
                "src/harnessix/secrets/redaction.py",
                "src/harnessix/session/sqlite.py",
                "src/harnessix/session/publication_seal.py",
                "tests/session/test_publication_seal.py",
                "tests/governance/test_security_governance_evidence.py",
            },
        ),
        (
            "query-publication-2026-09-28-v1",
            "contract-facts.json",
            {
                ".github/workflows/ci.yml",
                "docs/baselines/readability-0.9.0-final.json",
                "governance/documentation-policy-v1.json",
                "governance/readability-policy-v1.json",
                "pyproject.toml",
                "uv.lock",
                "scripts/documentation_check.py",
                "scripts/generate_specs.py",
                "scripts/license_scan.py",
                "scripts/readability_report.py",
                "scripts/sbom_generate.py",
                "scripts/secret_scan.py",
                "src/harnessix/agent/cancellation.py",
                "src/harnessix/agent/errors.py",
                "src/harnessix/agent/input_publication.py",
                "src/harnessix/agent/publication.py",
                "src/harnessix/agent/runtime.py",
                "src/harnessix/app_server/artifacts.py",
                "src/harnessix/app_server/query_runtime.py",
                "src/harnessix/app_server/command_runtime.py",
                "src/harnessix/app_server/frame_publication.py",
                "src/harnessix/app_server/server.py",
                "src/harnessix/app_server/service.py",
                "src/harnessix/app_server/stdio.py",
                "src/harnessix/product_config/runtime.py",
                "src/harnessix/product_config/server.py",
                "src/harnessix/protocol/contracts.py",
                "src/harnessix/protocol/projection.py",
                "src/harnessix/protocol/requests.py",
                "src/harnessix/sdk/agent_client.py",
                "src/harnessix/secrets/provider.py",
                "src/harnessix/secrets/publication.py",
                "src/harnessix/session/ports.py",
                "src/harnessix/session/sqlite.py",
                "tests/app_server/test_query_publication.py",
                "tests/product_config/test_query_publication_root.py",
                "tests/governance/test_security_governance_evidence.py",
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


def test_secret_publication_evidence_preserves_explicit_scope_and_release_boundaries():
    bundle = ROOT / "docs/validation/secret-publication-2026-09-27-v1"
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    verification = json.loads((bundle / "verification.json").read_bytes())
    assert facts["code_revision"] == review["code_revision"] == verification["code_revision"]
    assert facts["code_revision"] == ci["code_revision"]
    assert facts["special_tests"] == 62 and facts["new_special_tests"] == 56
    assert facts["prior_mcp_tests"] == 6
    assert facts["default_deny_missing_secret_capability"] is True
    assert facts["original_version_required"] is True
    assert facts["executor_and_publication_share_product_snapshot"] is True
    assert facts["environment_rotation_does_not_replace_running_snapshot"] is True
    assert facts["prepublication_and_rebuilt_owner_checked"] is True
    assert facts["values_keys_scalars_and_bounded_canonical_json_checked"] is True
    assert facts["token_timeout_and_parent_cancellation_before_owner"] is True
    assert facts["hash_only_secret_recovery_is_verified_metadata_only"] is True
    assert facts["owner_not_called_for_hash_only_secret_recovery"] is True
    assert facts["confirmed_audit_success_preserved"] is True
    assert facts["reexecutions_after_publication_failure"] == 0
    assert facts["actual_runtime_public_surfaces_nonempty"] is True
    assert facts["actual_mcp_client_checked"] is True
    assert facts["product_startup_and_normal_exit_release_scope"] is True
    assert facts["closed_snapshot_without_lease_has_failed_preflight_and_zero_run"] is True
    assert facts["container_owner_in_product_test"] == "contract_double"
    assert facts["real_container_or_windows_install_acceptance_claimed"] is False
    assert facts["all_provider_credentials_and_secret_end_to_end_closed"] is False
    assert facts["persisted_old_session_body_retroactively_cleaned"] is False
    assert facts["owner_artifact_internal_bytes_and_ownership_closed"] is False
    assert facts["cross_restart_secret_body_safely_restored"] is False
    assert facts["arbitrary_transformation_or_split_inference_closed"] is False
    assert facts["python_immutable_memory_erasure_claimed"] is False
    assert facts["hard_preemption_claimed"] is False
    assert facts["existing_generated_schema_drift"] is False
    assert facts["plan_binding_or_audit_migration"] is False
    assert facts["readability_policy_relaxed"] is False
    assert facts["new_package_dependencies"] == facts["new_package_dependency_edges"] == []
    assert facts["new_dependency_cycles"] == []
    assert all(not item["untracked_security_draft_present"] for item in facts["artifacts"])
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert review["current_ci_accepted"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_acceptance"] is False
    assert verification["make_check_passed"] is False
    assert verification["license_gate"]["blocked_archive_count"] == 12
    assert verification["external_model_requests"] == 0


def test_product_publication_evidence_keeps_current_epoch_and_recovery_limits():
    bundle = ROOT / "docs/validation/product-publication-2026-09-27-v1"
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    verification = json.loads((bundle / "verification.json").read_bytes())
    assert facts["code_revision"] == review["code_revision"] == ci["code_revision"]
    assert verification["code_revision"] == facts["code_revision"]
    assert facts["special_tests"] == facts["new_special_tests"] == 46
    assert facts["selected_provider_original_snapshot_shared"] is True
    assert facts["model_secret_has_no_process_injection_target"] is True
    assert facts["jsonl_complete_raw_and_decoded_shared_budget"] is True
    assert facts["runtime_sdk_model_history_and_otel_nonempty_checked"] is True
    assert facts["guard_and_proof_same_insert_transaction"] is True
    assert facts["legacy_other_epoch_and_tampered_proof_denied"] is True
    assert facts["real_old27_binary_and_atomic_migration28_preserve_bytes"] is True
    assert facts["confirmed_file_write_and_audit_preserved"] is True
    assert facts["reexecutions_after_publication_failure"] == 0
    assert facts["reconciliations_after_publication_failure"] == 0
    assert facts["migration_version"] == 28
    assert facts["ordinary_protocol_and_artifact_contracts_changed"] is False
    assert facts["new_package_dependency_edges"] == facts["new_dependency_cycles"] == []
    assert facts["readability_policy_relaxed"] is False
    assert facts["cross_restart_artifact_body_recovery_complete"] is False
    assert facts["historical_session_body_retroactively_cleaned"] is False
    assert facts["all_provider_credentials_and_public_outputs_closed"] is False
    assert facts["crypto_or_database_admin_tamper_proof_claimed"] is False
    assert facts["all_four_producers_business_integration_claimed"] is False
    assert facts["real_stdio_byte_transport_in_product_test_claimed"] is False
    assert facts["real_windows_container_or_install_acceptance_claimed"] is False
    assert facts["hard_preemption_or_python_immutable_erasure_claimed"] is False
    assert facts["reproducible_build_claimed"] is False
    assert all(not artifact["untracked_security_draft_present"] for artifact in facts["artifacts"])
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert review["current_ci_accepted"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_acceptance"] is False
    assert verification["external_model_requests"] == 0
    assert verification["new_automation_created"] is False
    assert verification["make_check_passed"] is False
    assert verification["license_gate"]["blocked_archive_count"] == 12


def test_typed_binary_evidence_preserves_contracts_and_platform_release_boundaries():
    bundle = ROOT / "docs/validation/typed-binary-publication-2026-09-28-v1"
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    verification = json.loads((bundle / "verification.json").read_bytes())
    assert facts["code_revision"] == review["code_revision"] == ci["code_revision"]
    assert verification["code_revision"] == facts["code_revision"]
    assert facts["formal_binary_purposes"] == ["action_output", "process_output"]
    assert facts["same_stream_cross_chunk_checked"] is True
    assert facts["decoded_and_original_work_budget_shared"] is True
    assert facts["model_protection_values_in_target_environment"] is False
    assert facts["owner_protection_precedes_lease_creation"] is True
    assert facts["owner_v1_schema_changed"] is False
    assert facts["database_migration_added"] is False
    assert facts["public_rejection_preserves_confirmed_effect_without_reexecution"] is True
    assert facts["effect_origin_independent_of_recovery_policy"] is True
    assert facts["cross_restart_artifact_body_recovery_complete"] is False
    assert facts["arbitrary_base64_field_decoding_claimed"] is False
    assert facts["real_windows_container_or_full_stdio_acceptance_claimed"] is False
    assert facts["all_provider_credentials_and_public_outputs_closed"] is False
    assert facts["readability_policy_relaxed"] is False
    assert facts["new_package_dependency_edges"] == facts["new_dependency_cycles"] == []
    assert facts["functional_new_tests"] == 91
    assert facts["special_tests"] == 108
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert review["current_ci_accepted"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_acceptance"] is False
    assert verification["external_model_requests"] == 0
    assert verification["new_automation_created"] is False
    assert verification["make_check_passed"] is False
    assert verification["full_regression"]["status"] in {"pending", "passed"}
    if verification["full_regression"]["status"] == "passed":
        assert verification["full_regression"]["tracked_inputs_unchanged_during_run"] is True
        assert verification["full_regression"]["untracked_attack_draft_excluded"] is True
    else:
        assert verification["full_regression"]["is_acceptance"] is False


def test_model_text_evidence_separates_new_publication_from_input_and_history_gaps():
    bundle = ROOT / "docs/validation/model-text-publication-2026-09-28-v1"
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    verification = json.loads((bundle / "verification.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    assert (
        facts["code_revision"]
        == verification["code_revision"]
        == review["code_revision"]
        == ci["code_revision"]
    )
    assert facts["special_tests"] == 110 and facts["functional_new_tests"] == 66
    assert facts["new_governance_tests"] == 2
    for field in (
        "whole_step_byte_and_work_budget",
        "per_content_id_window_utf8",
        "original_text_not_rewritten",
        "safe_prefix_before_response_completion",
        "actual_sdk_live_replay_sqlite_and_next_request_checked",
        "event_batches_guarded_before_cas",
        "main_and_summary_requests_guarded_before_provider",
        "attempt_intent_precedes_transport",
        "committed_usage_preserved_on_rejection",
        "compaction_summary_rejection_prevents_activation",
        "provider_and_guard_closed_on_cancel",
        "configured_protection_missing_stream_capability_denied",
    ):
        assert facts[field] is True
    for field in (
        "original_public_schemas_changed",
        "database_migration_added",
        "readability_policy_relaxed",
        "arbitrary_transformation_or_cross_content_inference_claimed",
        "legacy_session_replay_authorization_complete",
        "all_session_write_entries_guarded",
        "cross_restart_publication_proof_complete",
        "real_stdio_bytes_network_provider_or_three_platform_install_claimed",
    ):
        assert facts[field] is False
    assert facts["new_package_dependency_edges"] == facts["new_dependency_cycles"] == []
    old = facts["independent_clean_prior_revision_observation"]
    new = facts["current_direct_model_observation"]
    assert old["first_turn"] == "completed" and old["exposures"]["session_events"]
    assert new["first_turn"] == "failed" and new["first_failure"] == "public_output_secret_leak"
    assert new["second_turn"] == "completed" and not any(new["exposures"].values())
    assert new["provider_requests"] == new["closed_streams"] == 2
    assert old["real_model_requests"] == new["real_model_requests"] == 0
    assert new["telemetry_span_count"] > 0
    gap = facts["next_input_persistence_gap"]
    assert gap["provider_requests"] == 0 and gap["failure_code"] == "public_output_secret_leak"
    assert gap["input_persisted"] and gap["sdk_replay_contains_registered_value"]
    assert gap["sqlite_files_contain_registered_value"]
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert review["current_ci_accepted"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert (
        verification["external_model_requests"] == 0 and verification["make_check_passed"] is False
    )
    assert verification["license_gate"]["blocked_archive_count"] == 12
    full = verification["full_regression"]
    assert full["status"] in {"pending", "passed"}
    if full["status"] == "passed":
        assert full["tracked_inputs_unchanged_during_run"] is True
        assert full["untracked_attack_draft_excluded"] is True


def test_input_persistence_evidence_keeps_raw_protocol_history_and_release_gates_open():
    bundle = ROOT / "docs/validation/input-persistence-2026-09-28-v1"
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    verification = json.loads((bundle / "verification.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    assert (
        facts["code_revision"]
        == review["code_revision"]
        == verification["code_revision"]
        == ci["code_revision"]
    )
    assert facts["special_tests"] == 156 and facts["new_functional_tests"] == 57
    assert facts["new_governance_tests"] == 2 and facts["existing_special_tests"] == 99
    assert facts["known_raw_protocol_gap_tests"] == 2 and facts["known_raw_protocol_gaps_observed"]
    assert facts["full_raw_protocol_security_claimed"] is False
    assert facts["no_new_turn_receipt_or_provider_on_input_rejection"] is True
    assert facts["approval_checked_before_gateway_decision_audit"] is True
    assert facts["original_question_five_events_and_uuid5_preserved"] is True
    assert facts["trace_checked_before_new_operation"] is True
    assert facts["original_fields_fingerprints_and_schema_preserved"] is True
    assert facts["guarded_cache_rejection_preserves_historical_receipt"] is True
    assert facts["scope_lost_sdk_cancel_available"] is False
    assert facts["direct_cancel_and_host_drain_remain_independent"] is True
    assert facts["unknown_historical_authorization_closed"] is False
    assert facts["cross_restart_seal_closed"] is False
    assert facts["all_provider_credentials_closed"] is False
    assert facts["schema_migration"] is False and facts["hard_preemption_claimed"] is False
    assert facts["readability_policy_relaxed"] is False
    assert facts["new_package_dependencies"] == facts["new_dependency_cycles"] == []
    assert (
        facts["real_model_requests"] == 0
        and facts["full_product_or_network_provider_claimed"] is False
    )
    assert facts["license_archive_blockers"] == 12
    old = facts["independent_baseline"]
    fixed = facts["fixed_runtime_sqlite_sdk_observation"]
    assert old["code_revision"] == "45b801a0db99764b4ddcefa5da83a6ec9a12387e"
    assert old["module_source_verified"] and fixed["module_source_verified"]
    assert old["script_sha256"] == fixed["script_sha256"]
    assert len(old["records"]) == len(fixed["records"]) == 2
    assert all(
        item["input_persisted"] and item["sdk_replay_contains_registered_value"]
        for item in old["records"]
    )
    assert old["records"][1]["receipt_contains_registered_value"] is True
    assert all(
        item["error_code"] == "public_input_secret_leak"
        and item["provider_requests"] == 0
        and item["original_rejected_thread_unchanged"]
        and not item["request_receipt_exists"]
        and not item["sqlite_files_contain_registered_value"]
        and not item["sdk_replay_contains_registered_value"]
        for item in fixed["records"]
    )
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze" and ci["current_ci_accepted"] is False
    assert (
        ci["prior_success_is_current_acceptance"] is False
        and ci["waiting_for_ci_per_local_commit"] is False
    )


def test_protocol_frame_bundle_keeps_history_and_release_scope_explicit() -> None:
    bundle = ROOT / "docs/validation/protocol-frame-publication-2026-09-28-v1"
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    verification = json.loads((bundle / "verification.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    manifest = json.loads((bundle / "bundle-manifest.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    assert {item["code_revision"] for item in (facts, verification, review, manifest, ci)} == {
        manifest["code_revision"]
    }
    assert review["decision"] == "release_blocked"
    assert review["overall_0_9_complete"] is False
    assert facts["new_protocol_tests"] == 61 and facts["new_actual_cli_tests"] == 1
    assert facts["unknown_history_gap_observations"] == 1
    assert facts["special_regression"]["passed"] == 161
    assert facts["real_model_requests"] == 0
    assert facts["unknown_history_authorization_closed"] is False
    assert facts["direct_service_query_publication_closed"] is False
    assert facts["cross_restart_seal_closed"] is False
    assert facts["archive_rights_blockers"] == 12
    assert facts["historical_validation_files_unchanged"] is True
    old = {item["case"]: item for item in facts["independent_baseline"]["records"]}
    fixed = {item["case"]: item for item in facts["fixed_observation"]["records"]}
    assert facts["independent_baseline"]["module_source_verified"] is True
    assert facts["fixed_observation"]["module_source_verified"] is True
    for name in ("rpc_id", "param_key", "replay"):
        assert old[name]["response_contains_original_material"] is True
        assert fixed[name]["response_contains_original_material"] is False
        assert fixed[name]["private_history_unchanged"] is True
        assert fixed[name]["provider_requests"] == 0
    assert fixed["unknown_history"]["response_contains_original_material"] is True
    assert fixed["unknown_history"]["unknown_history_authorization_claimed"] is False
    assert facts["actual_product_cli"]["no_thread_or_turn_accepted"] is True
    assert facts["actual_product_cli"]["os_stdio_pipes"] is True
    assert facts["untracked_attack_draft_excluded"] is True
    full = verification["full_regression"]
    assert full["status"] in {"pending", "passed"}
    if full["status"] == "passed":
        assert full["passed"] == 5033 and full["skipped"] == 32
        assert full["tracked_inputs_unchanged_during_run"] is True
    else:
        assert full["is_acceptance"] is False
    assert verification["diagrams"]["each_png_visually_inspected"] is True
    assert verification["wheel_smoke"]["isolated_from_source_and_tests"] is True
    assert verification["wheel_smoke"]["original_sensitive_id_and_key_rejected"] is True
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_revision_acceptance"] is False


def test_query_bundle_keeps_current_scope_and_host_binding_limits_explicit() -> None:
    bundle = ROOT / "docs/validation/query-publication-2026-09-28-v1"
    manifest = json.loads((bundle / "bundle-manifest.json").read_bytes())
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    verification = json.loads((bundle / "verification.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    assert {item["code_revision"] for item in (manifest, facts, verification, review, ci)} == {
        manifest["code_revision"]
    }
    assert facts["new_query_tests"] == 61 and facts["new_product_root_tests"] == 1
    assert facts["new_governance_tests"] == 2 and facts["unknown_history_gap_observations"] == 1
    assert facts["special_regression"]["passed"] == 181
    assert facts["real_model_requests"] == 0
    assert facts["original_dto_hash_and_schema_unchanged"] is True
    assert facts["runtime_session_reader_object_binding_checked"] is True
    assert facts["request_store_physical_identity_proven"] is False
    assert facts["unknown_history_authorization_closed"] is False
    assert facts["internal_store_aggregate_authorization_closed"] is False
    assert facts["cross_restart_seal_closed"] is False
    assert facts["historical_validation_files_unchanged"] is True
    assert facts["untracked_attack_draft_excluded"] is True
    old, fixed = facts["independent_baseline"], facts["fixed_observation"]
    assert old["module_source_verified"] and fixed["module_source_verified"]
    assert old["script_sha256"] == fixed["script_sha256"]
    assert len(old["records"]) == len(fixed["records"]) == 5
    for before, after in zip(old["records"], fixed["records"], strict=True):
        assert before["surface"] == after["surface"]
        assert before["original_registered_material_exported"] is True
        assert after["original_registered_material_exported"] is False
        assert after["failure_code"] == "public_output_secret_leak"
        assert after["private_history_unchanged"] and after["provider_requests"] == 0
        assert after["transport_guard_used"] is False
    assert fixed["unknown_history"][0]["original_registered_material_exported"] is True
    assert fixed["unknown_history"][0]["unknown_history_authorization_claimed"] is False
    assert all(item["accepted"] for item in old["bindings"])
    assert all(not item["accepted"] for item in fixed["bindings"])
    assert facts["default_product_root"]["direct_service_no_transport_proxy"] is True
    assert facts["default_product_root"]["provider_requests"] == 0
    assert facts["default_product_root"]["original_materials_cleared"] is True
    assert facts["default_product_root"]["factory_and_stdio_driver_are_fixtures"] is True
    assert verification["diagrams"]["each_png_visually_inspected"] is True
    assert verification["wheel_smoke"]["isolated_from_source_and_tests"] is True
    assert verification["wheel_smoke"]["five_direct_queries_rejected_before_return"] is True
    assert verification["wheel_smoke"]["runtime_store_mismatch_rejected"] is True
    assert review["decision"] == "release_blocked" and not review["overall_0_9_complete"]
    assert facts["archive_rights_blockers"] == 12
    full = verification["full_regression"]
    assert full["status"] in {"pending", "passed"}
    metadata = (bundle / "README.md").read_text().split("---", 2)[1]
    if full["status"] == "pending":
        assert "\nstatus: draft\n" in metadata
        assert full["is_acceptance"] is False
        assert review["full_regression_acceptance_pending"] is True
    else:
        assert full["passed"] == 5097 and full["skipped"] == 32
        assert full["tracked_inputs_unchanged_during_run"] is True
        assert full["source_inputs_same_as_fixed_revision"] is True
        assert "\nstatus: current\n" in metadata
        assert review["full_regression_acceptance_pending"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_revision_acceptance"] is False


def test_event_seal_bundle_does_not_claim_default_history_authorization() -> None:
    bundle = ROOT / "docs/validation/event-seal-core-2026-09-28-v1"
    manifest = json.loads((bundle / "bundle-manifest.json").read_bytes())
    facts = json.loads((bundle / "contract-facts.json").read_bytes())
    verification = json.loads((bundle / "verification.json").read_bytes())
    review = json.loads((bundle / "review-packet.json").read_bytes())
    ci = json.loads((bundle / "ci-observation.json").read_bytes())
    assert {item["code_revision"] for item in (manifest, facts, verification, review, ci)} == {
        manifest["code_revision"]
    }
    assert facts["core_tests"] == 46 and facts["new_governance_tests"] == 2
    assert facts["real_model_requests"] == 0
    assert facts["production_key_backend_implemented"] is False
    assert facts["production_atomic_seal_write_implemented"] is False
    assert facts["default_product_history_authentication_enabled"] is False
    assert facts["snapshot_artifact_authentication_implemented"] is False
    assert facts["unknown_legacy_history_authorization_closed"] is False
    assert facts["cross_restart_production_acceptance_claimed"] is False
    assert facts["historical_validation_files_unchanged"] is True
    assert facts["archive_rights_blockers"] == 12
    baseline = facts["independent_default_root_gap"]
    assert baseline["module_source_verified"] is True
    assert len(baseline["default_root"]["records"]) == 5
    assert all(
        item["unregistered_old_material_exported"] for item in baseline["default_root"]["records"]
    )
    assert baseline["snapshot_hash_replacement"]["accepted"] is True
    assert baseline["history_authorization_closed"] is False
    consumer = verification["wheel_consumer"]
    assert consumer["passed"] is True and consumer["separate_os_processes"] is True
    assert consumer["fixture_persistent_key"] is True
    assert consumer["production_atomic_proof_claimed"] is False
    assert all(item["module_origin_verified"] for item in consumer["records"])
    assert consumer["records"][1]["same_original_bytes_verified"] is True
    assert verification["diagrams"]["each_png_visually_inspected"] is True
    assert verification["diagrams"]["diagrams"] == 5
    assert review["decision"] == "release_blocked" and review["overall_0_9_complete"] is False
    full = verification["full_regression"]
    metadata = (bundle / "README.md").read_text().split("---", 2)[1]
    assert full["status"] in {"pending", "passed"}
    if full["status"] == "pending":
        assert "\nstatus: draft\n" in metadata and full["is_acceptance"] is False
        assert review["full_regression_acceptance_pending"] is True
    else:
        assert full["passed"] >= 5145 and full["skipped"] == 32
        assert full["tracked_inputs_unchanged_during_run"] is True
        assert full["source_inputs_same_as_fixed_revision"] is True
        assert "\nstatus: current\n" in metadata
        assert review["full_regression_acceptance_pending"] is False
    assert ci["current_ci_status"] == "not_started_at_freeze"
    assert ci["prior_success_is_current_revision_acceptance"] is False
