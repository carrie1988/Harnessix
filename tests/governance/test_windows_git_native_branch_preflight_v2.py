"""有限阶段拒绝的离线合同；不运行Windows、下载资源或加载CDB。"""

from __future__ import annotations

import ast
import hashlib
import json
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.windows_git_native_branch_observation import contract, diagnostics, observe, preflight
from tests.governance.test_windows_git_native_branch_observation import (
    probe_record,
    project,
    witness,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/windows_git_native_branch_observation"
REAL_SELECTED_PATHS = preflight.selected_paths
FIELDS = {"first_failed_stage", "reason_code", "cdb_file_present", "interpreter_file_present"}
POLICY_ROWS = [
    (stage, reason)
    for stage, reasons in diagnostics.REASONS_BY_STAGE.items()
    for reason in sorted(reasons)
]


def test_original_sixteen_input_contract_is_still_exact():
    fixed = contract.read_contract()
    rows = contract.source_checks(ROOT, fixed)
    assert len(rows) == 16
    assert all(row["representation"] == "EXACT_FROZEN_BYTES" for row in rows)
    assert fixed["budgets"] == {
        "command_seconds": 20,
        "operation_seconds": 45,
        "workflow_step_seconds": 300,
        "outer_watchdog_seconds": 240,
    }
    assert len(fixed["selectors"]) == 2


@pytest.mark.parametrize("stage,reason", POLICY_ROWS)
def test_reason_is_exact_literal_from_actual_source_and_stage(stage, reason):
    literals = set()
    for name in ("contract.py", "identity.py", "preflight.py", "observe.py"):
        tree = ast.parse((SCRIPTS / name).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "ValueError"
                and len(node.exc.args) == 1
                and isinstance(node.exc.args[0], ast.Constant)
            ):
                literals.add(node.exc.args[0].value)
    assert reason in literals
    diagnostic = diagnostics.PreflightDiagnostic()
    diagnostic.enter(stage)
    diagnostic.capture_failure(ValueError(reason))
    assert diagnostic.snapshot() == {
        "first_failed_stage": stage,
        "reason_code": reason,
        "cdb_file_present": None,
        "interpreter_file_present": None,
    }


@pytest.mark.parametrize("stage", diagnostics.REASONS_BY_STAGE)
def test_unknown_error_does_not_export_any_dynamic_body_or_args(stage):
    diagnostic = diagnostics.PreflightDiagnostic()
    diagnostic.enter(stage)
    error = ValueError("SENTINEL_PRIVATE/path/body/key=not-a-real-key", {"body": "不可公开正文"})
    diagnostic.capture_failure(error)
    result = diagnostic.snapshot()
    assert set(result) == FIELDS
    assert result["first_failed_stage"] == stage and result["reason_code"] == "UNKNOWN"
    assert "SENTINEL_PRIVATE" not in json.dumps(result) and "正文" not in json.dumps(result)


def test_first_failure_is_not_overwritten_by_later_known_reason():
    diagnostic = diagnostics.PreflightDiagnostic()
    diagnostic.enter("source_identity")
    diagnostic.capture_failure(OSError("SENTINEL_PRIVATE"))
    diagnostic.enter("existing_tools")
    diagnostic.capture_failure(ValueError("existing_cdb_x64_required"))
    assert diagnostic.snapshot()["first_failed_stage"] == "source_identity"
    assert diagnostic.snapshot()["reason_code"] == "UNKNOWN"


def test_known_literal_at_wrong_stage_stays_unknown():
    diagnostic = diagnostics.PreflightDiagnostic()
    diagnostic.enter("selected_git")
    diagnostic.capture_failure(ValueError("existing_cdb_x64_required"))
    assert diagnostic.snapshot()["reason_code"] == "UNKNOWN"


def test_subclasses_and_hostile_string_conversion_are_not_invoked():
    class Hostile(ValueError):
        def __str__(self):
            pytest.fail("禁止格式化异常")

    class StringSubclass(str):
        def __eq__(self, other):
            pytest.fail("禁止调用动态字符串比较")

    for error in (
        Hostile("current_source_drift"),
        ValueError(StringSubclass("current_source_drift")),
    ):
        diagnostic = diagnostics.PreflightDiagnostic()
        diagnostic.enter("source_identity")
        diagnostic.capture_failure(error)
        assert diagnostic.snapshot()["reason_code"] == "UNKNOWN"


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_presence_accepts_only_actual_bool(value):
    diagnostic = diagnostics.PreflightDiagnostic()
    with pytest.raises(ValueError, match="diagnostic_presence_invalid"):
        diagnostic.presence("cdb_file_present", value)
    assert diagnostic.snapshot()["cdb_file_present"] is None


def test_invalid_stage_and_presence_field_cannot_expand_output():
    diagnostic = diagnostics.PreflightDiagnostic()
    with pytest.raises(ValueError, match="diagnostic_stage_invalid"):
        diagnostic.enter("SENTINEL_PRIVATE")
    with pytest.raises(ValueError, match="diagnostic_presence_invalid"):
        diagnostic.presence("SENTINEL_PRIVATE", True)
    assert set(diagnostic.snapshot()) == FIELDS
    assert diagnostic.snapshot()["first_failed_stage"] is None


@pytest.fixture
def offline_prepare(monkeypatch):
    # 只编排阶段故障；工具PE是合成头，pair/download为显式mock，不计现场身份通过。
    with tempfile.TemporaryDirectory(prefix="hx-native-preflight-v2-") as directory:
        root = Path(directory)
        repository = root / "repo"
        python = repository / ".venv/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"synthetic-interpreter")
        kits = root / "program-files"
        cdb = kits / "Windows Kits/10/Debuggers/x64/cdb.exe"
        cdb.parent.mkdir(parents=True)
        body = bytearray(128)
        body[:2] = b"MZ"
        struct.pack_into("<I", body, 0x3C, 64)
        body[64:68] = b"PE\0\0"
        struct.pack_into("<H", body, 68, 0x8664)
        cdb.write_bytes(body)
        monkeypatch.setattr(
            preflight, "os", SimpleNamespace(name="nt", environ={"ProgramFiles(x86)": str(kits)})
        )
        monkeypatch.setattr(preflight, "source_checks", lambda *args: [])
        paths = {"wrapper": root / "cmd/git.exe", "core": root / "mingw64/bin/git.exe"}
        monkeypatch.setattr(preflight, "selected_paths", lambda: (paths["wrapper"], paths))
        pdbs = {key: root / (key + ".pdb") for key in paths}
        monkeypatch.setattr(preflight, "download_symbols", lambda *args: pdbs)
        monkeypatch.setattr(preflight, "check_pair", lambda *args: {"matched": True})
        yield repository, root / "private", cdb, python


@pytest.mark.parametrize(
    "function,stage,reason",
    [
        ("source_checks", "source_identity", "current_source_drift"),
        ("selected_paths", "selected_git", "selected_git_missing"),
        ("existing_tools", "existing_tools", "existing_tools_missing_no_install_performed"),
        ("download_symbols", "symbols", "official_download_digest_mismatch"),
        ("check_pair", "pe_pdb_identity", "official_pair_guid_age_mismatch"),
        ("debugger_scripts", "scripts", "UNKNOWN"),
        ("sha256", "tool_identity", "UNKNOWN"),
    ],
)
def test_actual_prepare_stages_keep_refusal_nonzero_and_no_secret(
    offline_prepare, monkeypatch, tmp_path, function, stage, reason
):
    repository, output, _, _ = offline_prepare

    def fail(*args):
        raise ValueError(reason if reason != "UNKNOWN" else "SENTINEL_PRIVATE/body")

    monkeypatch.setattr(preflight, function, fail)
    monkeypatch.setattr(observe, "run_debugger", lambda *args: pytest.fail("禁止调试器执行"))
    report = tmp_path / "report"
    assert observe.run(repository, output, report, False, "") == 2
    result = json.loads((report / "result.json").read_bytes())
    assert result["schema"] == "harnessix.git-native-branch-observation/v2"
    assert result["status"] == "PREFLIGHT_REFUSED"
    assert result["execution_performed"] is False and not result["branch_gate_passed"]
    assert not result["original_sdk_acceptance"] and result["historical_root"] == "UNKNOWN"
    assert result["preflight_diagnostic"]["first_failed_stage"] == stage
    assert result["preflight_diagnostic"]["reason_code"] == reason
    assert "SENTINEL_PRIVATE" not in json.dumps(result)
    assert {f.name for f in report.iterdir()} == {"result.json", "result-sha256.json"}
    digest = json.loads((report / "result-sha256.json").read_bytes())["result_sha256"]
    assert digest == hashlib.sha256((report / "result.json").read_bytes()).hexdigest()


@pytest.mark.parametrize("missing", ["cdb", "python"])
def test_real_is_file_observations_preserve_original_short_circuit(
    offline_prepare, tmp_path, missing
):
    repository, output, cdb, python = offline_prepare
    (cdb if missing == "cdb" else python).unlink()
    report = tmp_path / "report"
    assert observe.run(repository, output, report, False, "") == 2
    diagnostic = json.loads((report / "result.json").read_bytes())["preflight_diagnostic"]
    assert diagnostic == {
        "first_failed_stage": "existing_tools",
        "reason_code": "existing_tools_missing_no_install_performed",
        "cdb_file_present": missing != "cdb",
        "interpreter_file_present": None if missing == "cdb" else False,
    }


@pytest.mark.parametrize(
    "variant,reason",
    [
        ("signature", "existing_cdb_pe_invalid"),
        ("x86", "existing_cdb_x64_required"),
        ("truncated", "UNKNOWN"),
    ],
)
def test_real_tool_header_failure_keeps_presence_distinct_from_identity(
    offline_prepare, tmp_path, variant, reason
):
    repository, output, cdb, _ = offline_prepare
    body = bytearray(cdb.read_bytes())
    if variant == "signature":
        body[:2] = b"XX"
    elif variant == "x86":
        struct.pack_into("<H", body, 68, 0x14C)
    else:
        body = bytearray(b"MZ")
    cdb.write_bytes(body)
    report = tmp_path / "report"
    assert observe.run(repository, output, report, False, "") == 2
    diagnostic = json.loads((report / "result.json").read_bytes())["preflight_diagnostic"]
    assert diagnostic["first_failed_stage"] == "existing_tools"
    assert diagnostic["reason_code"] == reason
    assert diagnostic["cdb_file_present"] is True and diagnostic["interpreter_file_present"] is True


def test_mocked_successful_dryrun_has_no_failure_not_native_success(
    offline_prepare, monkeypatch, tmp_path
):
    repository, output, _, _ = offline_prepare
    monkeypatch.setattr(observe, "run_debugger", lambda *args: pytest.fail("禁止执行"))
    report = tmp_path / "report"
    assert observe.run(repository, output, report, False, "") == 0
    result = json.loads((report / "result.json").read_bytes())
    assert result["status"] == "PREPARED_NOT_EXECUTED" and not result["execution_performed"]
    assert not result["branch_gate_passed"] and not result["original_sdk_acceptance"]
    assert result["preflight_diagnostic"] == {
        "first_failed_stage": None,
        "reason_code": None,
        "cdb_file_present": True,
        "interpreter_file_present": True,
    }


def test_actual_source_validator_reason_reaches_closed_report(
    offline_prepare, monkeypatch, tmp_path
):
    repository, output, _, _ = offline_prepare
    row = dict(contract.read_contract()["source_inputs"][0], sha256="0" * 64)
    monkeypatch.setattr(
        preflight,
        "source_checks",
        lambda *args: contract.source_checks(ROOT, {"source_inputs": [row]}),
    )
    report = tmp_path / "report"
    assert observe.run(repository, output, report, False, "") == 2
    result = json.loads((report / "result.json").read_bytes())
    assert result["preflight_diagnostic"]["first_failed_stage"] == "source_identity"
    assert result["preflight_diagnostic"]["reason_code"] == "current_source_drift"


def test_actual_selected_git_missing_reason_reaches_closed_report(
    offline_prepare, monkeypatch, tmp_path
):
    repository, output, _, _ = offline_prepare
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    # 撤销夹具的路径成功mock，复用本次实际导入的原函数。
    monkeypatch.setattr(preflight, "selected_paths", REAL_SELECTED_PATHS)
    report = tmp_path / "report"
    assert observe.run(repository, output, report, False, "") == 2
    result = json.loads((report / "result.json").read_bytes())
    assert result["preflight_diagnostic"]["first_failed_stage"] == "selected_git"
    assert result["preflight_diagnostic"]["reason_code"] == "selected_git_missing"


@pytest.mark.parametrize(
    "function,stage,reason",
    [
        ("source_checks", "source_identity", "current_source_drift"),
        ("selected_paths", "selected_git", "selected_git_missing"),
        ("check_pair", "pe_pdb_identity", "official_pair_sha_mismatch"),
        ("sha256", "tool_identity", "UNKNOWN"),
    ],
)
def test_recheck_refuses_before_launch_with_actual_stage(
    offline_prepare, monkeypatch, tmp_path, function, stage, reason
):
    repository, output, _, _ = offline_prepare
    original_prepare = observe.prepare

    def prepare_then_damage(*args):
        state = original_prepare(*args)

        def fail(*args):
            raise ValueError(reason)

        monkeypatch.setattr(preflight, function, fail)
        return state

    monkeypatch.setattr(observe, "prepare", prepare_then_damage)
    monkeypatch.setattr(observe, "execute_authorized", lambda *args: "a" * 40)
    monkeypatch.setattr(observe, "run_debugger", lambda *args: pytest.fail("禁止执行"))
    report = tmp_path / "report"
    assert observe.run(repository, output, report, True, "a" * 40) == 2
    result = json.loads((report / "result.json").read_bytes())
    assert result["status"] == "PREFLIGHT_REFUSED" and not result["execution_performed"]
    assert result["preflight_diagnostic"]["first_failed_stage"] == stage
    assert result["preflight_diagnostic"]["reason_code"] == reason


def test_nonwindows_platform_stage_is_actual_without_any_network(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(preflight, "download_symbols", lambda *args: pytest.fail("禁止网络"))
    report = tmp_path / "report"
    assert observe.run(ROOT, tmp_path / "private", report, False, "") == 2
    result = json.loads((report / "result.json").read_bytes())
    assert result["preflight_diagnostic"]["first_failed_stage"] == "platform_paths"
    assert result["preflight_diagnostic"]["reason_code"] == "windows_x64_required"


def test_metadata_and_revision_refusal_have_separate_first_stages(tmp_path, monkeypatch):
    def damaged():
        raise ValueError("offline_metadata_sha_mismatch")

    with monkeypatch.context() as patch:
        patch.setattr(observe, "read_contract", damaged)
        assert observe.run(ROOT, tmp_path / "private", tmp_path / "metadata", False, "") == 2
        result = json.loads((tmp_path / "metadata/result.json").read_bytes())
        assert result["status"] == "OFFLINE_METADATA_REFUSED"
        assert result["preflight_diagnostic"]["first_failed_stage"] == "metadata"
    monkeypatch.setattr(observe, "os", SimpleNamespace(name="posix", environ={}))
    assert observe.run(ROOT, tmp_path / "private", tmp_path / "revision", True, "a" * 40) == 2
    result = json.loads((tmp_path / "revision/result.json").read_bytes())
    assert result["preflight_diagnostic"]["first_failed_stage"] == "execution_revision"
    assert (
        result["preflight_diagnostic"]["reason_code"]
        == "explicit_fixed_revision_execution_required"
    )


def test_complete_mocked_observation_keeps_original_pytest_failure(
    offline_prepare, monkeypatch, tmp_path
):
    repository, output, _, _ = offline_prepare
    monkeypatch.setattr(observe, "execute_authorized", lambda *args: "a" * 40)

    def debugger(repository, output, state):
        state["debugger_started"] = True
        (output / "cdb-private.log").write_text(witness([(1, "FSTAT", -1), (2, "FSTAT", -1)]))
        (output / "cases.json").write_text(
            json.dumps(
                {
                    "pytest_exit": 1,
                    "invalid": False,
                    "cases": [project(probe_record(case)) for case in ("A", "B")],
                }
            )
        )
        return {"debugger_exit": 1, "timed_out": False, "log_limit_stopped": False}

    monkeypatch.setattr(observe, "run_debugger", debugger)
    report = tmp_path / "report"
    assert observe.run(repository, output, report, True, "a" * 40) == 1
    result = json.loads((report / "result.json").read_bytes())
    assert result["branch_gate_passed"] and result["pytest_exit"] == 1
    assert not result["original_sdk_acceptance"] and result["historical_root"] == "UNKNOWN"
    assert result["preflight_diagnostic"]["first_failed_stage"] is None
