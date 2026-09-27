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
