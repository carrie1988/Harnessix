"""0.9.4b供应链扫描脚本的正反例回归。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.license_decisions import decide
from scripts.sbom_generate import build_sbom, canonical_bytes, validate_sbom
from scripts.secret_scan import scan_paths

ROOT = Path(__file__).resolve().parents[2]


def _run(module: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        (sys.executable, "-m", module, *args),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
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
    assert len(names) == 73
    assert document["bomFormat"] == "CycloneDX" and document["specVersion"] == "1.5"
    validate_sbom(document)


def test_sbom_check_fails_on_drift(tmp_path: Path) -> None:
    drift = tmp_path / "sbom.json"
    drift.write_text("{}\n", encoding="utf-8")
    completed = _run("scripts.sbom_generate", "--check", "--output", str(drift))
    assert completed.returncode == 1
    assert "漂移" in completed.stderr


def test_sbom_check_passes_for_committed_file() -> None:
    completed = _run("scripts.sbom_generate", "--check")
    assert completed.returncode == 0


def test_license_report_blocks_unknown_and_denied() -> None:
    policy = {"allow": ["MIT"], "deny": ["GPL-3.0-only"]}
    assert decide("GPL-3.0-only", policy)[0] == "violation"
    assert decide("UNKNOWN", policy)[0] == "violation"


def test_license_and_logic_requires_all_parts() -> None:
    policy = {"allow": ["MIT"], "deny": []}
    assert decide("MIT AND Apache-2.0", policy)[0] == "violation"
    policy["allow"] = ["MIT", "Apache-2.0"]
    assert decide("MIT AND Apache-2.0", policy)[0] == "allow"


def test_committed_license_report_keeps_restricted_archives_blocked() -> None:
    # 工具正确性与发布资格不同：测试证明失败关闭，CI许可门禁仍真实失败。
    completed = _run("scripts.license_scan", "--check")
    report = json.loads((ROOT / "governance/license-scan-v2.json").read_bytes())
    assert completed.returncode == 1, completed.stderr
    assert report["violation_count"] == 12
    assert "许可证违规" in completed.stderr


def test_secret_rules_hit_sensitive_and_miss_benign(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b'api_key = "' + b"A" * 32 + b'"')
    benign = tmp_path / "benign.txt"
    benign.write_bytes(b'api_key = "${ENVIRONMENT_REFERENCE}"\n')
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


def test_make_install_consumes_locked_dependencies() -> None:
    install = (ROOT / "Makefile").read_text().split("install:\n", 1)[1].split("\n\n", 1)[0]
    assert "uv sync --locked --all-extras --dev" in install
