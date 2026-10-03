"""两处原生分支候选的离线负对照；不执行Windows、CDB或发行Git。"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import struct
import uuid
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts.windows_git_native_branch_observation import contract, identity, observe, preflight
from scripts.windows_git_native_branch_observation.projection import (
    PROBE_PREFIX,
    branch_records,
    project_case,
)
from scripts.windows_git_native_branch_observation.run_cases import CaseSink

ROOT = Path(__file__).resolve().parents[2]
CORE = next(row for row in contract.read_contract()["pairs"] if row["role"] == "core")


@pytest.fixture
def synthetic_pair(tmp_path):
    # 最小结构仅验证解析算法；其身份与发行binary完全不同，不作为原生见证。
    guid = uuid.UUID(CORE["guid"]).bytes_le
    body = bytearray(0x110000)
    body[:2] = b"MZ"
    struct.pack_into("<I", body, 0x3C, 0x80)
    body[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", body, 0x84, 0x8664, 1)
    struct.pack_into("<H", body, 0x94, 0xF0)
    struct.pack_into("<H", body, 0x98, 0x20B)
    struct.pack_into("<I", body, 0x98 + 56, CORE["size_of_image"])
    struct.pack_into("<II", body, 0x98 + 112 + 6 * 8, 0x1100, 28)
    struct.pack_into("<IIII", body, 0x188 + 8, 0x100000, 0x1000, 0x100000, 0x200)
    struct.pack_into("<IIII", body, 0x300 + 12, 2, 32, 0, 0x380)
    body[0x380:0x39C] = b"RSDS" + guid + struct.pack("<I", 1) + b"git\0"
    for rva, machine_bytes in CORE["branch_bytes"].items():
        offset = int(rva, 16) - 0x1000 + 0x200
        body[offset : offset + 7] = bytes.fromhex(machine_bytes)
    executable = tmp_path / "synthetic.exe"
    executable.write_bytes(body)
    records = bytearray()
    for name, rva in CORE["symbol_rvas"].items():
        payload = struct.pack("<IIH", 0, int(rva, 16) - 0x1000, 1) + name.encode() + b"\0"
        records += struct.pack("<HH", len(payload) + 2, 0x110E) + payload
    info = struct.pack("<III", 20000404, 0, 1) + guid
    dbi = bytearray(64)
    struct.pack_into("<H", dbi, 20, 4)
    pdb = bytearray(6 * 512)
    pdb[:32] = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"
    directory = struct.pack("<9I", 5, 0, 28, 0, 64, len(records), 3, 4, 5)
    struct.pack_into("<6I", pdb, 32, 512, 0, 6, len(directory), 0, 1)
    struct.pack_into("<I", pdb, 512, 2)
    pdb[1024 : 1024 + len(directory)] = directory
    pdb[1536 : 1536 + len(info)] = info
    pdb[2048 : 2048 + len(dbi)] = dbi
    pdb[2560 : 2560 + len(records)] = records
    symbols = tmp_path / "synthetic.pdb"
    symbols.write_bytes(pdb)
    expected = dict(
        CORE,
        pe_sha256=identity.sha256(body),
        pdb_sha256=identity.sha256(pdb),
        pe_bytes=len(body),
        pdb_bytes=len(pdb),
    )
    return executable, symbols, expected


def test_reused_identity_algorithm_matches_synthetic_pair_not_official_binary(synthetic_pair):
    executable, symbols, expected = synthetic_pair
    assert identity.check_pair(executable, symbols, expected)["matched"]
    assert expected["pe_sha256"] != CORE["pe_sha256"]
    assert expected["pdb_sha256"] != CORE["pdb_sha256"]


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("guid", "00000000-0000-0000-0000-000000000000", "guid_age"),
        ("age", 2, "guid_age"),
        ("pe_sha256", "0" * 64, "sha_mismatch"),
        ("pdb_sha256", "0" * 64, "sha_mismatch"),
        ("pe_bytes", 1, "size_mismatch"),
        ("size_of_image", 1, "image_size"),
        ("symbol_rvas", dict(CORE["symbol_rvas"], hash_fd="0x70b41"), "symbol_rva"),
        ("branch_bytes", dict(CORE["branch_bytes"], **{"0x70b74": "00" * 7}), "machine_bytes"),
    ],
)
def test_identity_drift_is_refused(synthetic_pair, field, value, error):
    executable, symbols, expected = synthetic_pair
    with pytest.raises(ValueError, match=error):
        identity.check_pair(executable, symbols, dict(expected, **{field: value}))


@pytest.mark.parametrize("kind", ["pe", "pdb"])
def test_invalid_signature_is_refused(tmp_path, kind):
    path = tmp_path / kind
    path.write_bytes(b"x" * 64)
    parser = identity.pe_identity if kind == "pe" else identity.pdb_identity
    with pytest.raises(ValueError, match="signature_invalid"):
        parser(path)


def witness(events):
    lines, armed = [], set()
    for pid, phase, value in events:
        if pid not in armed:
            lines.append(f"FHX_NATIVE_ARM pid={pid}")
            armed.add(pid)
        rva = {"FSTAT": "70b74", "INDEX": "70bc1"}[phase]
        lines.append(
            f"FHX_NATIVE_BRANCH pid={pid} tid=1 phase={phase} rva={rva} "
            f"fd=0 path=0 flags=1 ret={value}"
        )
    return "\n".join(lines)


@pytest.mark.parametrize(
    "events,expected",
    [
        ([(1, "FSTAT", -1), (2, "FSTAT", -1)], True),
        ([(1, "FSTAT", 0), (1, "INDEX", -1), (2, "FSTAT", 0), (2, "INDEX", -1)], True),
        ([(1, "FSTAT", 0), (1, "INDEX", 0), (2, "FSTAT", 0), (2, "INDEX", 0)], True),
        ([(1, "FSTAT", -1), (2, "FSTAT", 0), (2, "INDEX", -1)], True),
        ([(1, "FSTAT", 0), (2, "FSTAT", -1)], False),
        ([(1, "INDEX", -1), (1, "FSTAT", 0), (2, "FSTAT", -1)], False),
        ([(1, "FSTAT", -1), (1, "FSTAT", -1), (2, "FSTAT", -1)], False),
        ([(1, "FSTAT", -1), (2, "FSTAT", -1), (3, "FSTAT", -1)], False),
        ([(1, "FSTAT", 2147483648), (2, "FSTAT", -1)], False),
    ],
)
def test_actual_branch_sequence_rules(events, expected):
    assert branch_records(witness(events))["two_material_invocations_witnessed"] is expected


@pytest.mark.parametrize(
    "old,new",
    [
        ("rva=70b74", "rva=70b75"),
        ("fd=0", "fd=1"),
        ("path=0", "path=1"),
        ("flags=1", "flags=0"),
        ("FHX_NATIVE_ARM pid=1\n", ""),
        ("FHX_NATIVE_BRANCH", ".echo FHX_NATIVE_BRANCH"),
    ],
)
def test_wrong_context_and_command_echo_cannot_be_gate(old, new):
    log = witness([(1, "FSTAT", -1), (2, "FSTAT", -1)]).replace(old, new)
    assert not branch_records(log)["two_material_invocations_witnessed"]


def test_arm_only_and_printf_template_are_not_observation():
    for log in (
        "FHX_NATIVE_ARM pid=1\nFHX_NATIVE_ARM pid=2",
        '.printf "FHX_NATIVE_BRANCH pid=%u ret=%d", @$tpid, @eax',
    ):
        assert not branch_records(log)["two_material_invocations_witnessed"]


def probe_record(case="A"):
    raw = {"bytes": 0, "sha256": "a" * 64, "eof": True, "full_raw_verified": True}
    receipt_raw = {"observed_bytes": 0, "sha256": "a" * 64, "eof": True}
    return {
        "schema": "harnessix.minimum-commit-probe/v5",
        "selector": case,
        "platform": "win32",
        "installed_hooks": 13,
        "diagnostic_incomplete": True,
        "diagnostic_truncated": False,
        "outcomes": {"setup": "passed", "call": "failed", "teardown": "passed"},
        "operations": [
            {
                "kind": "write",
                "lease": {"returncode": 2},
                "post_worker_failure": {"git_returncode": 128},
                "post_proof": {"status": "absent"},
                "post_raw": {"stdout": raw, "stderr": raw},
                "post_receipt": {
                    "provenance": "terminal_receipt_authenticated",
                    "raw_stdout": receipt_raw,
                    "raw_stderr": receipt_raw,
                },
            }
        ],
        "unexpected": "Secret正文不得输出",
    }


def project(record):
    return project_case(PROBE_PREFIX + json.dumps(record))


def test_v5_projection_keeps_failure_without_body_or_digest():
    result = project(probe_record())
    assert result["worker_return"] == 2 and result["git_return"] == 128
    assert result["proof"] == "ABSENT" and result["call"] == "failed"
    assert result["raw_validation"] == "OBSERVED_V5_VERIFIED_NOT_INDEPENDENT_MAC"
    assert "Secret" not in json.dumps(result) and "sha256" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("installed_hooks", True),
        ("installed_hooks", 14),
        ("platform", "darwin"),
        ("schema", "harnessix.minimum-commit-probe/v4"),
        ("selector", "C"),
    ],
)
def test_probe_wrong_contract_refused(field, value):
    with pytest.raises(ValueError, match="probe_frame_invalid"):
        project(dict(probe_record(), **{field: value}))


def test_raw_missing_eof_or_sha_match_is_not_verified():
    record = copy.deepcopy(probe_record())
    record["operations"][0]["post_raw"]["stdout"]["eof"] = False
    assert project(record)["raw_validation"] == "UNAVAILABLE"
    record = copy.deepcopy(probe_record())
    record["operations"][0]["post_receipt"]["raw_stdout"]["sha256"] = "b" * 64
    assert project(record)["raw_validation"] == "UNAVAILABLE"


def test_sink_only_keeps_two_bounded_probe_frames():
    sink = CaseSink()
    sink.write("Secret正文\n")
    for case in ("A", "B", "A"):
        sink.write(PROBE_PREFIX + json.dumps(probe_record(case)) + "\n")
    assert len(sink.rows) == 2 and sink.invalid
    assert "Secret" not in json.dumps(sink.rows)
    sink = CaseSink()
    sink.write("x" * 65537)
    assert sink.invalid and not sink.pending


def test_metadata_sha_is_checked_before_download(tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    path.write_bytes(contract.CONTRACT_PATH.read_bytes() + b" ")
    monkeypatch.setattr(contract.urllib.request, "build_opener", lambda *args: pytest.fail("下载"))
    with pytest.raises(ValueError, match="metadata_sha"):
        contract.read_contract(path)


def test_exact_crlf_representation_and_source_drift(tmp_path):
    path = tmp_path / "contract.json"
    path.write_bytes(contract.CONTRACT_PATH.read_bytes().replace(b"\n", b"\r\n"))
    assert contract.read_contract(path) == contract.read_contract()
    row = contract.read_contract()["source_inputs"][0]
    target = tmp_path / row["path"]
    target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / row["path"]).read_bytes().replace(b"\n", b"\r\n"))
    assert contract.source_checks(tmp_path, {"source_inputs": [row]})[0]["representation"] == (
        "EXACT_LF_TO_CRLF_TRANSFORM"
    )
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ValueError, match="source_drift"):
        contract.source_checks(tmp_path, {"source_inputs": [row]})


def test_download_digest_rejects_before_zip_extraction(tmp_path, monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(b"bad")

    monkeypatch.setattr(contract.urllib.request, "build_opener", lambda *args: Opener())
    fixed = copy.deepcopy(contract.read_contract())
    fixed["assets"]["symbols"]["bytes"] = 3
    with pytest.raises(ValueError, match="download_digest"):
        contract.download_symbols(tmp_path, fixed)
    assert not (tmp_path / "symbols").exists()


def test_only_fixed_members_are_extracted_and_verified(tmp_path, monkeypatch):
    fixed = copy.deepcopy(contract.read_contract())
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        for row in fixed["pairs"]:
            body = row["role"].encode()
            archive.writestr(row["pdb_member"], body)
            row.update(pdb_bytes=len(body), pdb_sha256=hashlib.sha256(body).hexdigest())
        archive.writestr("../../must-not-extract", b"Secret")
    body = blob.getvalue()
    fixed["assets"]["symbols"].update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())

    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(body)

    monkeypatch.setattr(contract.urllib.request, "build_opener", lambda *args: Opener())
    paths = contract.download_symbols(tmp_path, fixed)
    assert set(paths) == {"wrapper", "core"}
    assert {path.name for path in tmp_path.rglob("*") if path.is_file()} == {"git.pdb"}


def test_hardware_scripts_preserve_auto_continue_and_full_machine_guards(tmp_path):
    preflight.debugger_scripts(tmp_path, CORE)
    loaded = (tmp_path / "on-git-load.cdb").read_text()
    assert loaded.count("ba e 1") == 2
    assert ") { .if ((dwo(git+0x70b6f" in loaded
    assert "wo(git+0x70b74+0x4)" in loaded and "by(git+0x70bc1+0x6)" in loaded
    assert "git!mingw_fstat-git == 0x315ca0" in loaded
    for phase in ("fstat", "index"):
        handler = (tmp_path / ("on-" + phase + ".cdb")).read_text()
        assert "@ebx == 0" in handler and "@rdi == 0" in handler
        assert "@rip-git" in handler and handler.endswith("gc\n")
        for forbidden in (".call", ".dump", " db ", " bp ", "%ma", "%mu"):
            assert forbidden not in handler
    assert ".childdbg 1" in (tmp_path / "bootstrap.cdb").read_text()


def test_nonwindows_preflight_and_explicit_execution_are_refused(tmp_path):
    if observe.os.name == "nt":
        pytest.skip("仅非Windows入口负例")
    with pytest.raises(ValueError, match="windows_x64"):
        preflight.prepare(ROOT, tmp_path / "private", contract.read_contract())
    with pytest.raises(ValueError, match="execution_required"):
        observe.execute_authorized("d" * 40)
    assert not (tmp_path / "private").exists()


def test_default_dryrun_does_not_launch_debugger(tmp_path, monkeypatch):
    monkeypatch.setattr(
        observe,
        "prepare",
        lambda *args: {
            "source_checks": [None] * 16,
            "cdb_sha256": "a" * 64,
            "python_sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(observe, "run_debugger", lambda *args: pytest.fail("执行"))
    report = tmp_path / "report"
    assert observe.run(ROOT, tmp_path / "private", report, False, "") == 0
    result = json.loads((report / "result.json").read_text())
    assert result["status"] == "PREPARED_NOT_EXECUTED"
    assert not result["branch_gate_passed"] and not result["execution_performed"]
    assert not result["original_sdk_acceptance"] and result["historical_root"] == "UNKNOWN"


def test_helper_success_is_not_actual_branch_gate(tmp_path):
    (tmp_path / "cases.json").write_text(
        json.dumps(
            {
                "pytest_exit": 0,
                "invalid": False,
                "cases": [project(probe_record(case)) for case in ("A", "B")],
            }
        )
    )
    (tmp_path / "cdb-private.log").write_text("FHX_NATIVE_ARM pid=1\nFHX_NATIVE_ARM pid=2\n")
    execution = {"debugger_exit": 0, "timed_out": False, "log_limit_stopped": False}
    assert not observe.observation_result(tmp_path, execution)["branch_gate_passed"]
    (tmp_path / "cdb-private.log").write_text(witness([(1, "FSTAT", -1), (2, "FSTAT", -1)]))
    assert observe.observation_result(tmp_path, execution)["branch_gate_passed"]
    execution["debugger_exit"] = 3
    assert not observe.observation_result(tmp_path, execution)["branch_gate_passed"]
    execution["debugger_exit"] = 0
    execution["timed_out"] = True
    assert not observe.observation_result(tmp_path, execution)["branch_gate_passed"]


def test_failure_cases_keep_nonzero_exit_even_when_branch_gate_is_complete(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    (private / "cdb-private.log").write_text(witness([(1, "FSTAT", -1), (2, "FSTAT", -1)]))
    (private / "cases.json").write_text(
        json.dumps(
            {
                "pytest_exit": 1,
                "invalid": False,
                "cases": [project(probe_record(case)) for case in ("A", "B")],
            }
        )
    )
    monkeypatch.setattr(
        observe,
        "prepare",
        lambda *args: {
            "source_checks": [None] * 16,
            "cdb_sha256": "a" * 64,
            "python_sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(observe, "execute_authorized", lambda *args: "d" * 40)
    monkeypatch.setattr(observe, "recheck", lambda *args: None)

    def simulated_started_debugger(repository, output, state):
        state["debugger_started"] = True
        return {"debugger_exit": 1, "timed_out": False, "log_limit_stopped": False}

    monkeypatch.setattr(observe, "run_debugger", simulated_started_debugger)
    report = tmp_path / "report"
    assert observe.run(ROOT, private, report, True, "d" * 40) == 1
    result = json.loads((report / "result.json").read_text())
    assert result["branch_gate_passed"] and result["pytest_exit"] == 1
    assert result["historical_run_result"] == "FAIL_RETAINED"
    assert not result["original_sdk_acceptance"] and result["historical_root"] == "UNKNOWN"


def test_case_extra_body_field_is_rejected_before_publication(tmp_path):
    cases = [project(probe_record(case)) for case in ("A", "B")]
    cases[0]["body"] = "Secret正文"
    (tmp_path / "cases.json").write_text(
        json.dumps(
            {
                "pytest_exit": 1,
                "invalid": False,
                "cases": cases,
            }
        )
    )
    (tmp_path / "cdb-private.log").write_text(witness([(1, "FSTAT", -1), (2, "FSTAT", -1)]))
    execution = {"debugger_exit": 1, "timed_out": False, "log_limit_stopped": False}
    result = observe.observation_result(tmp_path, execution)
    assert not result["branch_gate_passed"] and result["cases"] == []
    assert "Secret" not in json.dumps(result)


def test_noninteger_pytest_return_cannot_publish_text(tmp_path):
    (tmp_path / "cases.json").write_text(
        json.dumps(
            {
                "pytest_exit": "Secret正文",
                "invalid": False,
                "cases": [project(probe_record(case)) for case in ("A", "B")],
            }
        )
    )
    execution = {"debugger_exit": 1, "timed_out": False, "log_limit_stopped": False}
    result = observe.observation_result(tmp_path, execution)
    assert result["pytest_exit"] is None and not result["branch_gate_passed"]
    assert "Secret" not in json.dumps(result)


def test_wrong_first_phase_does_not_fabricate_fstat_return():
    result = branch_records(witness([(1, "INDEX", -1), (2, "FSTAT", -1)]))
    assert not result["two_material_invocations_witnessed"]
    assert result["invocations"][0]["fstat_return"] is None


def test_launch_failure_does_not_claim_native_execution(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setattr(
        observe,
        "prepare",
        lambda *args: {
            "source_checks": [],
            "cdb_sha256": "a" * 64,
            "python_sha256": "b" * 64,
            "command": ["not-started"],
        },
    )
    monkeypatch.setattr(observe, "execute_authorized", lambda *args: "d" * 40)
    monkeypatch.setattr(observe, "recheck", lambda *args: None)

    def unavailable(*args, **kwargs):
        raise OSError("不得公开的启动错误")

    monkeypatch.setattr(observe.subprocess, "Popen", unavailable)
    report = tmp_path / "report"
    assert observe.run(ROOT, private, report, True, "d" * 40) == 2
    result = json.loads((report / "result.json").read_text())
    assert not result["execution_performed"] and not result["branch_gate_passed"]
    assert "不得公开" not in json.dumps(result)


def test_watchdog_stops_debugger_without_leave_alive_flag(tmp_path, monkeypatch):
    class Process:
        killed = False

        def poll(self):
            return 1 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            assert timeout == 5 and self.killed
            return 1

    process = Process()
    command = ["existing-cdb"]
    monkeypatch.setattr(observe.subprocess, "Popen", lambda *args, **kwargs: process)
    times = iter([0, 240])
    monkeypatch.setattr(observe.time, "monotonic", lambda: next(times))
    result = observe.run_debugger(ROOT, tmp_path, {"command": command})
    assert result["timed_out"] and process.killed and "-pd" not in command


def test_workflow_is_manual_single_windows_job_with_exact_budgets_and_no_raw_upload():
    body = (ROOT / ".github/workflows/windows-git-native-branch-observation.yml").read_text()
    workflow = yaml.safe_load(body)
    trigger = workflow.get("on", workflow.get(True))
    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert inputs["execute"]["default"] is False and inputs["execute"]["type"] == "boolean"
    assert len(workflow["jobs"]) == 1 and workflow["permissions"] == {"contents": "read"}
    job = next(iter(workflow["jobs"].values()))
    assert job["runs-on"] == "windows-latest" and "strategy" not in job
    observation = next(step for step in job["steps"] if step.get("timeout-minutes") == 5)
    assert "--execute" in observation["run"] and "NATIVE_EXPECTED_REVISION" in observation["run"]
    upload = job["steps"][-1]["with"]["path"]
    assert "private" not in upload and "result.json" in upload and "result-sha256.json" in upload
    assert "persist-credentials: false" in body
    assert contract.read_contract()["budgets"] == {
        "command_seconds": 20,
        "operation_seconds": 45,
        "workflow_step_seconds": 300,
        "outer_watchdog_seconds": 240,
    }
    assert len(contract.read_contract()["source_inputs"]) == 18
