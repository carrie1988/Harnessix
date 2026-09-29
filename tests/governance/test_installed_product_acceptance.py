"""安装验收必须拒绝源码借用、制品漂移和存量运行目录。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
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
    assert {step["id"] for step in uploads} == {"installed-evidence", "upgrade-evidence"}
    installed = next(step for step in uploads if step["id"] == "installed-evidence")
    paths = installed["with"]["path"].splitlines()
    assert len(paths) == 7
    assert all(line.endswith((".json", ".txt", ".log")) for line in paths)
    assert not any("/case" in line or "*" in line for line in paths)


def test_three_platform_jobs_consume_one_scanned_canonical_wheel() -> None:
    import yaml

    path = Path(__file__).parents[2] / ".github/workflows/installed-product-acceptance.yml"
    jobs = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
    builder = jobs["canonical-wheel"]
    consumer = jobs["installed-product"]
    assert builder["runs-on"] == "ubuntu-latest"
    assert builder["outputs"]["wheel-sha256"] == "${{ steps.wheel-identity.outputs.sha256 }}"
    assert sum("uv build" in step.get("run", "") for step in builder["steps"]) == 1
    assert any(
        "scripts/secret_scan.py --artifact-dir" in step.get("run", "") for step in builder["steps"]
    )
    assert consumer["needs"] == "canonical-wheel"
    assert not any("uv build" in step.get("run", "") for step in consumer["steps"])
    upload = next(step for step in builder["steps"] if "upload-artifact@" in step.get("uses", ""))
    download = next(
        step for step in consumer["steps"] if "download-artifact@" in step.get("uses", "")
    )
    assert upload["with"]["name"] == download["with"]["name"]
    assert upload["with"]["path"] == "${{ runner.temp }}/harnessix-canonical-wheel/*.whl"
    assert upload["with"]["if-no-files-found"] == "error"
    assert download["uses"] == "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
    wheel_input = next(step for step in consumer["steps"] if step.get("id") == "wheel-input")
    assert (
        wheel_input["env"]["CANONICAL_WHEEL_SHA256"]
        == "${{ needs.canonical-wheel.outputs.wheel-sha256 }}"
    )


@pytest.mark.parametrize("changed", [False, True])
def test_wheel_input_requires_the_builder_digest_before_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    import yaml

    path = Path(__file__).parents[2] / ".github/workflows/installed-product-acceptance.yml"
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["installed-product"]["steps"]
    step = next(step for step in steps if step.get("id") == "wheel-input")
    # 执行实际工作流中的输入生成代码；篡改时不能先生成可供pip安装的输入。
    program = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    (tmp_path / "wheel").mkdir()
    wheel = tmp_path / "wheel/fixture.whl"
    original = b"canonical-wheel-fixture"
    wheel.write_bytes(b"different-wheel-fixture" if changed else original)
    expected = hashlib.sha256(original).hexdigest()
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(tmp_path))
    monkeypatch.setenv("CANONICAL_WHEEL_SHA256", expected)
    requirement = tmp_path / "wheel-requirement.txt"
    if changed:
        with pytest.raises(AssertionError, match="canonical_wheel_mismatch"):
            exec(compile(program, "workflow-wheel-input", "exec"), {})
        assert not requirement.exists()
    else:
        exec(compile(program, "workflow-wheel-input", "exec"), {})
        assert (
            requirement.read_text(encoding="utf-8")
            == f"{wheel.as_uri()} --hash=sha256:{expected}\n"
        )


def test_canonical_checkout_overrides_crlf_only_for_the_checkout_process(tmp_path: Path) -> None:
    import yaml

    path = Path(__file__).parents[2] / ".github/workflows/installed-product-acceptance.yml"
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["installed-product"]["steps"]
    checkout = next(step for step in steps if "checkout@" in step.get("uses", ""))
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True, timeout=30)
    (source / ".gitattributes").write_bytes(b"* text=auto\n")
    original = b"value = 1\n"
    (source / "fixture.py").write_bytes(original)
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True, timeout=30)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Acceptance",
            "-c",
            "user.email=acceptance@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=source,
        check=True,
        capture_output=True,
        timeout=30,
    )
    config = tmp_path / "fixture.gitconfig"
    settings = b"[core]\n\tautocrlf = true\n\teol = crlf\n"
    config.write_bytes(settings)
    # 使用自有配置夹具模拟Windows默认值，不改用户全局Git设置。
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}
    common = ["git", "clone", "-q", "--no-hardlinks", str(source)]
    control = tmp_path / "control"
    subprocess.run(
        [*common, str(control)], env=environment, check=True, capture_output=True, timeout=30
    )
    assert (control / "fixture.py").read_bytes() == original.replace(b"\n", b"\r\n")
    canonical = tmp_path / "canonical"
    subprocess.run(
        [*common, str(canonical)],
        env={**environment, **checkout["env"]},
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert (canonical / "fixture.py").read_bytes() == original
    assert (control / "fixture.py").read_bytes() == original.replace(b"\n", b"\r\n")
    assert config.read_bytes() == settings


def test_frozen_diagnostic_is_data_but_active_source_formatting_and_secret_scan_remain(
    tmp_path: Path,
) -> None:
    from scripts.secret_scan import _tracked_files, scan_paths

    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(Path(__file__).parents[2] / "pyproject.toml", project / "pyproject.toml")
    diagnostic = project / "docs/validation/fixture/diagnostics/probe.py"
    diagnostic.parent.mkdir(parents=True)
    diagnostic.write_bytes(b"canary='Bearer " + b"A" * 32 + b"'\n")
    original = diagnostic.read_bytes()
    installed_fixture = project / "docs/validation/fixture/installation/acceptance.py"
    installed_fixture.parent.mkdir()
    installed_fixture.write_bytes(
        b"import subprocess\nasync def accept():\n    subprocess.run(['fixture'])\n"
    )
    installed_original = installed_fixture.read_bytes()
    active = project / "active.py"
    active.write_bytes(b"value=1\n")
    ruff = shutil.which("ruff")
    assert ruff is not None
    failed = subprocess.run(
        [ruff, "format", "--check", str(project)], capture_output=True, text=True, timeout=30
    )
    assert failed.returncode == 1 and "active.py" in failed.stdout
    assert "probe.py" not in failed.stdout and "acceptance.py" not in failed.stdout
    active.write_bytes(b"value = 1\n")
    subprocess.run(
        [ruff, "format", "--check", str(project)], check=True, capture_output=True, timeout=30
    )
    assert diagnostic.read_bytes() == original
    subprocess.run([ruff, "check", str(project)], check=True, capture_output=True, timeout=30)
    assert installed_fixture.read_bytes() == installed_original
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    assert diagnostic in _tracked_files(project)
    assert installed_fixture in _tracked_files(project)
    assert {finding["rule"] for finding in scan_paths([diagnostic])} == {"bearer_literal"}


def test_windows_state_focus_preserves_all_files_and_original_step_deadlines() -> None:
    import yaml

    path = Path(__file__).parents[2] / ".github/workflows/ci.yml"
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["windows-trusted-execution"][
        "steps"
    ]
    groups = {
        "验证原生完整业务状态备份与观察者边界": [
            "tests/product_config/test_product_state_backup.py",
            "tests/product_config/test_state_fixture_readiness.py",
        ],
        "验证原生完整业务状态恢复与元数据边界": [
            "tests/product_config/test_product_state_restore.py",
            "tests/product_config/test_windows_metadata_contracts.py",
        ],
    }
    selected = [step for step in steps if step.get("name") in groups]
    assert len(selected) == 2
    for step in selected:
        # 原文件各执行一次；诊断、断言与期限不因编排分组而被删除。
        assert step["timeout-minutes"] == 5
        assert step["run"].split() == [
            "uv",
            "run",
            "pytest",
            *groups[step["name"]],
            "-vv",
            "-o",
            "faulthandler_timeout=60",
        ]
    assert {step["name"] for step in selected} == set(groups)


def test_current_installation_examples_match_package_version_and_locked_inputs() -> None:
    import tomllib

    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    current = (
        (root / "docs/operations/installation.md")
        .read_text(encoding="utf-8")
        .split("### 5.3", 1)[0]
    )
    assert f"当前包版本为`{project['version']}`" in current
    assert f"harnessix-{project['version']}-py3-none-any.whl" in current
    assert "旧兼容Action" not in current
    assert "--require-hashes --no-deps" in current
    assert "--no-emit-project" in current
    assert "pip install 'harnessix[tui,openai]'" not in current
