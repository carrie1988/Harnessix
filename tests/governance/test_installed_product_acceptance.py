"""安装验收必须拒绝源码借用、制品漂移和存量运行目录。"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.installed_product_acceptance import (
    AcceptanceFailure,
    check_environment,
    check_package_members,
    prepare_case,
    state_snapshot,
)


def test_installed_environment_refuses_source_import_and_nonisolated_process(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    root = tmp_path / "acceptance"
    prefix = root / "venv"
    for paths, isolated in (([str(source / "src")], True), ([], False)):
        with pytest.raises(AcceptanceFailure, match="installed_environment_invalid"):
            check_environment(root, source, prefix=prefix, isolated=isolated, paths=paths)


@pytest.mark.parametrize("location", ["source", "source/acceptance"])
def test_acceptance_root_cannot_be_inside_source_checkout(tmp_path: Path, location: str) -> None:
    source = tmp_path / "source"
    root = tmp_path / location
    with pytest.raises(AcceptanceFailure, match="installed_environment_invalid"):
        check_environment(root, source, prefix=root / "venv", isolated=True, paths=[])


def test_installed_environment_requires_the_specific_new_virtual_environment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "acceptance"
    with pytest.raises(AcceptanceFailure, match="installed_environment_invalid"):
        check_environment(
            root, tmp_path / "source", prefix=tmp_path / "another-venv", isolated=True, paths=[]
        )
    check_environment(
        root, tmp_path / "source", prefix=root / "venv", isolated=True, paths=[str(root / "venv")]
    )


def test_installed_package_is_checked_against_actual_wheel_bytes(tmp_path: Path) -> None:
    package = tmp_path / "installed/harnessix"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b'"""installed fixture"""\n')
    wheel = tmp_path / "fixture.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("harnessix/__init__.py", (package / "__init__.py").read_bytes())
        archive.writestr("fixture.dist-info/METADATA", b"Version: 0.1.0\n")
    assert check_package_members(wheel, package) == 1
    (package / "__init__.py").write_bytes(b"changed installed bytes\n")
    with pytest.raises(AcceptanceFailure, match="installed_package_mismatch"):
        check_package_members(wheel, package)


@pytest.mark.parametrize("member", ["harnessix/../outside.py", "harnessix/../../outside.py"])
def test_wheel_package_members_cannot_escape_package(tmp_path: Path, member: str) -> None:
    wheel = tmp_path / "fixture.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(member, b"outside\n")
    with pytest.raises(AcceptanceFailure, match="installed_package_mismatch"):
        check_package_members(wheel, tmp_path / "installed")


def test_state_snapshot_detects_missing_and_changed_bytes_without_exporting_body(
    tmp_path: Path,
) -> None:
    case = tmp_path / "case"
    case.mkdir()
    key = case / "key-fixture"
    key.write_bytes(b"synthetic-state-key-body")
    before = state_snapshot(case)
    assert b"synthetic-state-key-body" not in repr(before).encode()
    key.write_bytes(b"changed-fixture")
    assert state_snapshot(case) != before
    key.unlink()
    assert state_snapshot(case) != before


def test_existing_case_is_not_deleted_or_reused(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    sentinel = case / "preserved.txt"
    sentinel.write_bytes(b"original fixture")
    with pytest.raises(FileExistsError):
        prepare_case(tmp_path)
    assert sentinel.read_bytes() == b"original fixture"


def test_installed_acceptance_workflow_uploads_no_private_state() -> None:
    import yaml

    path = Path(__file__).parents[2] / ".github/workflows/installed-product-acceptance.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    job = workflow["jobs"]["installed-product"]
    assert {entry["platform"] for entry in job["strategy"]["matrix"]["include"]} == {
        "linux",
        "macos",
        "windows",
    }
    assert job["strategy"]["fail-fast"] is False
    uploads = [step for step in job["steps"] if "upload-artifact@" in step.get("uses", "")]
    assert len(uploads) == 1
    paths = uploads[0]["with"]["path"].splitlines()
    assert len(paths) == 7
    assert all(line.endswith((".json", ".txt", ".log")) for line in paths)
    assert not any("/case" in line or "*" in line for line in paths)
