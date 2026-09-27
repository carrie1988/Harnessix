"""SBOM正式格式、锁定身份、离线来源及干净目录的正反例。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import ValidationError

from scripts.sbom_generate import SCHEMA_ROOT, build_sbom, canonical_bytes, validate_sbom

ROOT = Path(__file__).resolve().parents[2]


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "fixture-app"\nversion = "1.0"\nlicense = "AGPL-3.0-only"\n'
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "fixture-app"\nversion = "1.0"\nsource = {editable = "."}\n'
        'dependencies = [{name = "fixture-lib"}]\n'
        '[[package]]\nname = "fixture-lib"\nversion = "2.0"\n'
        'source = {registry = "https://pypi.org/simple"}\n'
        'sdist = {url = "https://example.com/lib.tar.gz", hash = "sha256:' + "a" * 64 + '"}\n',
        encoding="utf-8",
    )
    return lock, project


def test_sbom_graph_and_archive_identity_matches_all_locked_inputs() -> None:
    sbom = build_sbom(ROOT / "uv.lock")
    validate_sbom(sbom)
    identities = {sbom["metadata"]["component"]["bom-ref"]} | {
        item["bom-ref"] for item in sbom["components"]
    }
    assert len(identities) == 74
    assert identities == {item["ref"] for item in sbom["dependencies"]}
    assert all(set(item["dependsOn"]) <= identities for item in sbom["dependencies"])
    for item in sbom["components"]:
        assert item["purl"] == item["bom-ref"] == f"pkg:pypi/{item['name']}@{item['version']}"
        assert "hashes" not in item and "scope" not in item
        assert item["externalReferences"]
    assert sum(len(item["externalReferences"]) for item in sbom["components"]) == 777


@pytest.mark.parametrize(
    "field,value", [("serialNumber", "urn:harnessix:sbom:uv-lock"), ("bomFormat", "garbage")]
)
def test_official_schema_rejects_invalid_bom_fields(tmp_path: Path, field: str, value: str) -> None:
    lock, project = _inputs(tmp_path)
    sbom = build_sbom(lock, project)
    sbom[field] = value
    with pytest.raises(ValidationError):
        validate_sbom(sbom)


def test_official_schema_rejects_nonstandard_hash_algorithm(tmp_path: Path) -> None:
    sbom = build_sbom(*_inputs(tmp_path))
    sbom["components"][0]["externalReferences"][0]["hashes"][0]["alg"] = "SHA256"
    with pytest.raises(ValidationError):
        validate_sbom(sbom)


def test_declared_spec_version_must_match_pinned_schema(tmp_path: Path) -> None:
    sbom = build_sbom(*_inputs(tmp_path))
    sbom["specVersion"] = "1.6"
    with pytest.raises(ValueError, match="版本"):
        validate_sbom(sbom)


@pytest.mark.parametrize("change", ["version", "source", "orphan", "credential", "hash"])
def test_unreviewed_or_inconsistent_inputs_fail_closed(tmp_path: Path, change: str) -> None:
    lock, project = _inputs(tmp_path)
    body = lock.read_text()
    replacements = {
        "version": ('version = "1.0"', 'version = "1.1"'),
        "source": ("https://pypi.org/simple", "https://other.example/simple"),
        "orphan": (
            'dependencies = [{name = "fixture-lib"}]',
            'dependencies = [{name = "missing"}]',
        ),
        "credential": (
            "https://example.com/lib.tar.gz",
            "https://user:canary@example.com/lib.tar.gz",
        ),
        "hash": ("sha256:" + "a" * 64, "sha256:bad"),
    }
    before, after = replacements[change]
    lock.write_text(body.replace(before, after), encoding="utf-8")
    with pytest.raises(ValueError):
        build_sbom(lock, project)


def test_cli_check_works_without_dist_or_installed_project(tmp_path: Path) -> None:
    lock, project = _inputs(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("sbom_generate.py", "cli_console.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    shutil.copytree(SCHEMA_ROOT, tmp_path / "governance/schemas/cyclonedx-1.5")
    expected = tmp_path / "governance/sbom.cyclonedx.json"
    expected.write_bytes(canonical_bytes(build_sbom(lock, project)))
    completed = subprocess.run(
        [sys.executable, str(scripts / "sbom_generate.py"), "--check"],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / "dist").exists()
    expected.unlink()
    missing = subprocess.run(
        [sys.executable, str(scripts / "sbom_generate.py"), "--check"],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert missing.returncode == 1
    assert not expected.exists()  # 检查不得先生成再掩盖缺失。


def test_vendored_schema_bytes_match_fixed_provenance() -> None:
    provenance = json.loads((SCHEMA_ROOT / "PROVENANCE.json").read_bytes())
    assert provenance["upstream_revision"] == "c320fc0f0b46873864927d9d5684eea7ba439728"
    for entry in provenance["files"]:
        assert (
            hashlib.sha256((SCHEMA_ROOT / entry["name"]).read_bytes()).hexdigest()
            == entry["sha256"]
        )
