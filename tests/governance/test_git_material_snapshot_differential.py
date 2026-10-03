"""四格对照的准入、命令与工作流约束；不替代真实Windows或原SDK门。"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from tests.product_config import test_git_material_snapshot_differential as differential
from tests.product_config.git_minimum_commit_probe import SELECTORS

ROOT = Path(__file__).resolve().parents[2]
make_process = differential.material_tests.make_process


@dataclass(frozen=True)
class _Request:
    """仅测试派生参数的不变性，不冒充正式材料、manifest或批准。"""

    git_argv: tuple[str, ...]
    expiry_monotonic_ns: int
    marker: object
    repo_path: str = "private"
    git_environment: tuple[tuple[str, str], ...] = (("LANG", "C"),)
    expected_oid: str = "a" * 64


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("remaining", [-1, 0, 5_000_000_000, 45_000_000_000])
def test_derived_request_changes_only_write_flag_and_never_renews_deadline(
    monkeypatch, write, remaining
):
    now, marker = 80_000_000_000, object()
    monkeypatch.setattr(differential.time, "monotonic_ns", lambda: now)
    argv = (
        "git",
        "--git-dir=private",
        "hash-object",
        "--no-filters",
        "-t",
        "commit",
        "-w",
        "--stdin",
    )
    original = _Request(argv, now + remaining, marker)
    derived = differential._diagnostic_request(original, write=write)
    assert derived.git_argv == (argv if write else argv[:-2] + argv[-1:])
    assert derived.expiry_monotonic_ns == now + min(remaining, 20_000_000_000)
    assert original == _Request(argv, now + remaining, marker)
    assert derived.repo_path == original.repo_path
    assert derived.git_environment is original.git_environment
    assert derived.expected_oid == original.expected_oid
    assert not hasattr(derived, "purpose_digest") and not hasattr(derived, "marker")


@pytest.mark.parametrize("flags", [(), ("-w", "-w")])
def test_derived_request_refuses_missing_or_ambiguous_write_flag(flags):
    with pytest.raises(AssertionError):
        differential._diagnostic_request(_Request(flags, 1, object()), write=False)


@pytest.mark.parametrize("phase", ["effect", "identity"])
def test_real_final_checks_cannot_pass_after_original_operation_deadline(
    make_process, tmp_path, monkeypatch, phase
):
    original_prepare = differential.material_tests._prepare
    original_effect = differential._object_bytes
    original_identity = differential._checked_executable
    budgets = []
    counts = {"effect": 0, "identity": 0}
    expired = []

    def prepare(*args, **kwargs):
        # 真实短预算仅用于负例，不修改默认45秒或正式请求构造器。
        kwargs["budget"] = differential.material_tests.GitOperationBudget(1.0)
        prepared = original_prepare(*args, **kwargs)
        budgets.append(prepared.budget)
        return prepared

    def after_deadline(name, original, *args):
        counts[name] += 1
        if name == phase and counts[name] == 2:
            time.sleep(max(0, budgets[0].expires_at_monotonic_ns / 1e9 - time.monotonic()) + 0.02)
            expired.append(name)
        return original(*args)

    monkeypatch.setattr(differential.material_tests, "_prepare", prepare)
    monkeypatch.setattr(
        differential,
        "_object_bytes",
        lambda *args: after_deadline("effect", original_effect, *args),
    )
    monkeypatch.setattr(
        differential,
        "_checked_executable",
        lambda: after_deadline("identity", original_identity),
    )
    with pytest.raises(KernelError) as raised:
        differential.test_real_snapshot_hash_and_write(make_process, tmp_path, False, False)
    assert expired == [phase] and raised.value.code == "git_process_timeout"


def test_real_counterfactuals_reuse_original_io_without_new_output_collector():
    source = inspect.getsource(differential.test_real_snapshot_hash_and_write)
    for required in (
        "_command(request, resources, windows)",
        "_namespace(request, resources, windows)",
        "_snapshot(request, resources, windows)",
        "GitMaterialFailureObservation()",
        "redirect_stderr(null)",
        "os.write(snapshot.fileno()",
        "prepared.budget.remaining()",
        "staged.remove()",
        '"cat-file", "commit"',
    ):
        assert required in source
    for forbidden in ("subprocess.Popen", "subprocess.run", "PIPE", "check_pair =", "print("):
        assert forbidden not in source
    assert source.count("_git(") == 1 and source.count("_checked_executable()") == 2


def test_explicit_windows_diagnostic_requires_original_source_pe_and_pdb_authority():
    source = inspect.getsource(differential._checked_executable)
    for required in (
        'HARNESSIX_GIT_NATIVE_DIFFERENTIAL") != "1"',
        'os.environ["HARNESSIX_GIT_NATIVE_DIFFERENTIAL_SYMBOLS"]',
        "contract.read_contract()",
        "contract.source_checks(ROOT, fixed)",
        "preflight.selected_paths()",
        "identity.check_pair(",
    ):
        assert required in source
    assert source.index("pytest.skip") < source.index("contract.read_contract")
    assert "except" not in source and "download" not in source


def test_manual_workflow_has_four_independent_steps_and_keeps_original_sdk_failure_gate():
    workflow = (ROOT / ".github/workflows/windows-git-minimum-commit-probe.yml").read_text()
    for case in ("hash-direct", "hash-held", "write-direct", "write-held"):
        selector = (
            "tests/product_config/test_git_material_snapshot_differential.py"
            f"::test_real_snapshot_hash_and_write[{case}]"
        )
        assert workflow.count(selector) == 1
    assert workflow.count("timeout-minutes: 1") == 4
    assert workflow.count("if: ${{ !cancelled() && steps.preflight.conclusion == 'success' }}") == 5
    for original in SELECTORS:
        assert workflow.count(original) == 1
    for required in (
        "id: preflight",
        "scripts.windows_git_native_branch_observation.observe",
        "HARNESSIX_GIT_NATIVE_DIFFERENTIAL: '1'",
        "HARNESSIX_GIT_NATIVE_DIFFERENTIAL_SYMBOLS:",
        "--tb=no",
        "--show-capture=no",
        "timeout-minutes: 5",
    ):
        assert required in workflow
    for forbidden in (
        "--execute",
        "continue-on-error",
        "upload-artifact",
        "--junitxml",
        "--showlocals",
    ):
        assert forbidden not in workflow
