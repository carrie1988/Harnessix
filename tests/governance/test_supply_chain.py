"""0.9.4b供应链扫描脚本的正反例回归。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.license_scan import build_report, locked_names
from scripts.sbom_generate import build_sbom, canonical_bytes
from scripts.secret_scan import RULES, scan_paths

ROOT = Path(__file__).resolve().parents[2]


def _run(module: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        (sys.executable, "-m", module, *args),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_sbom_is_deterministic_and_complete() -> None:
    lock = ROOT / "uv.lock"
    first = canonical_bytes(build_sbom(lock))
    second = canonical_bytes(build_sbom(lock))
    assert first == second
    document = json.loads(first)
    names = [item["name"] for item in document["components"]]
    assert names == sorted(names)
    assert len(names) >= 70
    assert document["bomFormat"] == "CycloneDX" and document["specVersion"] == "1.5"


def test_sbom_check_fails_on_drift(tmp_path: Path) -> None:
    drift = tmp_path / "sbom.json"
    drift.write_text("{}\n", encoding="utf-8")
    completed = _run("scripts.sbom_generate", "--check", "--output", str(drift))
    assert completed.returncode == 1
    assert "漂移" in completed.stderr


def test_sbom_check_passes_for_committed_file() -> None:
    completed = _run("scripts.sbom_generate", "--check")
    assert completed.returncode == 0


def test_license_report_blocks_unknown_and_denied(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n[[package]]\nname = "evil-lib"\nversion = "1.0"\n'
        '[[package]]\nname = "unknown-lib"\nversion = "2.0"\n',
        encoding="utf-8",
    )
    policy = {
        "spec_version": "harnessix.license-policy/v1",
        "allow": ["MIT"],
        "deny": ["GPL-3.0-ONLY"],
        "overrides": [{"name": "evil-lib", "declared_license": "GPL-3.0-only"}],
    }
    report = build_report(lock, policy)
    statuses = {entry["name"]: entry["status"] for entry in report["entries"]}
    assert statuses == {"evil-lib": "violation", "unknown-lib": "violation"}
    assert report["violation_count"] == 2


def test_license_and_logic_requires_all_parts(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n[[package]]\nname = "dual-lic"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    policy = {
        "spec_version": "harnessix.license-policy/v1",
        "allow": ["MIT"],
        "deny": [],
        "overrides": [{"name": "dual-lic", "declared_license": "MIT AND Apache-2.0"}],
    }
    report = build_report(lock, policy)
    assert report["violation_count"] == 1
    policy["allow"] = ["MIT", "APACHE-2.0"]
    assert build_report(lock, policy)["violation_count"] == 0


def test_committed_license_report_is_clean() -> None:
    completed = _run("scripts.license_scan", "--check")
    assert completed.returncode == 0, completed.stderr


def test_secret_rules_hit_sensitive_and_miss_benign(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b'api_key = "AbCdEfGhIjKlMnOpQrStUvWx123456"')
    benign = tmp_path / "benign.txt"
    benign.write_bytes(b"api_key = \"${ENVIRONMENT_REFERENCE}\"\n")
    findings = scan_paths([secret, benign])
    assert [item["path"] for item in findings] == [str(secret)]
    assert findings[0]["rule"] == "generic_api_assignment"


def test_secret_scan_self_check_and_repo_clean() -> None:
    assert _run("scripts.secret_scan", "--self-check").returncode == 0
    completed = _run("scripts.secret_scan")
    assert completed.returncode == 0, completed.stderr


def test_workflow_actions_are_sha_pinned() -> None:
    workflows = sorted((ROOT / ".github/workflows").glob("*.yml"))
    assert workflows
    for path in workflows:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "uses:" not in stripped:
                continue
            uses = stripped.split("uses:", 1)[1].strip()
            uses = uses.split("#", 1)[0].strip()
            action, _, ref = uses.partition("@")
            if action.startswith("."):
                continue
            assert len(ref) == 40 and all(char in "0123456789abcdef" for char in ref), (
                f"{path.name}: {uses} 未按完整SHA固定"
            )
