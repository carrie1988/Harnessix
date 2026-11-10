"""完整原生材料验收只迁移调度；冻结原选择器与其他Windows步骤。"""

from __future__ import annotations

import shlex
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "8162953c80035ffea1cb7b9f6fc23995e3d2bbfb"
ORIGINAL_STEP = "验证原生认证raw回执与Git基准"
INPUT_FILES = {
    "tests/product_config/test_git_object_material.py",
    "tests/product_config/test_git_material_input.py",
    "tests/product_config/test_git_material_snapshot_lifecycle.py",
    "tests/product_config/test_git_material_native.py",
    "tests/product_config/test_git_material_directory_access.py",
    "tests/product_config/test_git_material_owner_exit.py",
}
CAS_FILES = {
    "tests/delivery/test_git_material_cas.py",
    "tests/delivery/test_cas_write_authority.py",
    "tests/delivery/test_git_object_references.py",
    "tests/delivery/test_git_tree_closure.py",
    "tests/product_config/test_git_material_cas_integration.py",
}


@pytest.fixture(scope="module")
def original_job():
    body = subprocess.run(
        ["git", "show", f"{BASELINE}:.github/workflows/ci.yml"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return yaml.safe_load(body)["jobs"]["windows-trusted-execution"]


def _workflow():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def test_ci_source_decoding_is_explicit_utf8(monkeypatch):
    original = Path.read_text

    def require_utf8(path, *args, **kwargs):
        if path == ROOT / ".github/workflows/ci.yml":
            assert kwargs.get("encoding") == "utf-8"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", require_utf8)
    assert "windows-git-native-material" in _workflow()["jobs"]


def _original_selectors(original_job):
    step = next(step for step in original_job["steps"] if step.get("name") == ORIGINAL_STEP)
    command = shlex.split(step["run"])
    assert command[:3] == ["uv", "run", "pytest"]
    assert command[-3:] == ["-vv", "-o", "faulthandler_timeout=60"]
    return command[3:-3]


def test_original_native_selectors_are_retained_exactly_once(original_job):
    rows = _workflow()["jobs"]["windows-git-native-material"]["strategy"]["matrix"]["include"]
    assert [row["group"] for row in rows] == ["authenticated-raw", "object-input", "cas-reference"]
    selected = [selector for row in rows for selector in shlex.split(row["selectors"])]
    original = _original_selectors(original_job)
    assert len(original) == len(set(original)) == 27
    assert Counter(selected) == Counter(original)


@pytest.mark.parametrize(
    "group,count", [("authenticated-raw", 16), ("object-input", 6), ("cas-reference", 5)]
)
def test_groups_keep_original_order_and_fixed_module_boundary(original_job, group, count):
    rows = _workflow()["jobs"]["windows-git-native-material"]["strategy"]["matrix"]["include"]
    row = next(row for row in rows if row["group"] == group)
    selected = shlex.split(row["selectors"])
    original = _original_selectors(original_job)
    expected = {
        "authenticated-raw": [item for item in original if item not in INPUT_FILES | CAS_FILES],
        "object-input": [item for item in original if item in INPUT_FILES],
        "cas-reference": [item for item in original if item in CAS_FILES],
    }[group]
    assert selected == expected and len(selected) == count


def test_original_windows_core_steps_are_unchanged(original_job):
    current = _workflow()["jobs"]["windows-trusted-execution"]
    expected = {
        **original_job,
        "steps": [step for step in original_job["steps"] if step.get("name") != ORIGINAL_STEP],
    }
    assert current == expected


def test_matrix_keeps_native_runner_and_failure_semantics(original_job):
    workflow = _workflow()
    job = workflow["jobs"]["windows-git-native-material"]
    assert workflow["permissions"] == {"contents": "read"}
    assert job["runs-on"] == "windows-latest"
    assert job["strategy"]["fail-fast"] is False
    assert job["steps"][:3] == original_job["steps"][:3]
    assert len(job["steps"]) == 4
    step = job["steps"][-1]
    assert step["timeout-minutes"] == 5
    assert step["run"] == "uv run pytest ${{ matrix.selectors }} -vv -o faulthandler_timeout=60"
    assert "continue-on-error" not in job and "continue-on-error" not in step
    assert not any(key in job for key in ("env", "secrets", "permissions", "needs"))
