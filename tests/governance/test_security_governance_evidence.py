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
