"""安全治理阶段证据清单完整性；原字节和固定源码输入不可静默漂移。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "docs/validation/security-governance-2026-09-27-v1"


def test_security_governance_bundle_files_and_source_inputs_match_manifest() -> None:
    manifest = json.loads((BUNDLE / "bundle-manifest.json").read_bytes())
    entries = manifest["files"]
    names = [entry["path"] for entry in entries]
    assert len(names) == len(set(names)) == 5
    assert set(names) == {
        "README.md",
        "verification.json",
        "sbom-facts.json",
        "ci-observation.json",
        "review-packet.json",
    }
    for entry in entries:
        path = Path(entry["path"])
        assert not path.is_absolute() and ".." not in path.parts
        body = (BUNDLE / path).read_bytes()
        assert len(body) == entry["size_bytes"]
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
    for entry in manifest["source_inputs"]:
        result = subprocess.run(
            ["git", "show", f"{entry['code_revision']}:{entry['path']}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=10,
        )
        assert hashlib.sha256(result.stdout).hexdigest() == entry["sha256"]
