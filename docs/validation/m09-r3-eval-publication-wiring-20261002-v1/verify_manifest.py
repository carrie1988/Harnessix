"""只读校验独立交付包的路径、权限与 SHA，不修改任何封存记录。"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    manifest_path = root / "manifest.json"
    assert not manifest_path.is_symlink()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    manifest = json.loads(manifest_path.read_text())
    expected = {item["path"] for item in manifest["members"]}
    assert len(expected) == len(manifest["members"])
    failures = []
    for item in manifest["members"]:
        path = root / item["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            failures.append({"path": item["path"], "error": "invalid_path"})
            continue
        if not path.is_file():
            failures.append({"path": item["path"], "error": "missing_file"})
            continue
        if (
            hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
            or path.stat().st_size != item["bytes"]
            or stat.S_IMODE(path.stat().st_mode) != 0o600
        ):
            failures.append({"path": item["path"], "error": "identity_or_mode_mismatch"})
    files = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            failures.append({"path": str(path.relative_to(root)), "error": "symlink"})
        elif path.is_dir():
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                failures.append({"path": str(path.relative_to(root)), "error": "directory_mode"})
        elif path.is_file():
            files.add(path.relative_to(root).as_posix())
    if files - expected - {"manifest.json", "verification.json"}:
        failures.append({"error": "unlisted_file"})
    result = {
        "schema": "harnessix.r3-auth-wiring-verification/v1",
        "status": "PASS" if not failures else "FAIL",
        "member_count": len(expected),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "failures": failures,
        "mutating_operations": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
