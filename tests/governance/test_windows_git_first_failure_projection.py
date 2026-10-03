"""首失败九字段的v2发布接缝和严格v1兼容；仅使用离线有限合成记录。"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from harnessix.delivery import git_material_failure as worker
from harnessix.delivery.git_material_input_contracts import GitMaterialInputError
from scripts.windows_git_native_branch_observation import contract, observe, projection, run_cases
from scripts.windows_git_native_branch_observation import failure_projection as failure
from tests.governance.test_git_minimum_commit_probe import _signal_post_operation
from tests.governance.test_windows_git_native_failure_projection import (
    CANARY,
    EXECUTION,
    frame,
    record,
    without_new_observation,
    write_report,
)
from tests.governance.test_windows_git_trace2_input_binding import _metadata_changes
from tests.product_config import git_minimum_commit_probe as probe

ROOT = Path(__file__).resolve().parents[2]
BASE = "f07263ce3d4ddb304b2ff054044f86f26d5267c6"
V2 = "harnessix.git-native-failure-observation/v2"
FIRST_FIELDS = {
    "schema",
    "stage",
    "origin",
    "error_code",
    "handler_error_code",
    "git_popen_returned",
    "git_returncode",
    "git_stdout_complete",
    "git_stdout_expected",
}
METADATA = "scripts/windows_git_native_branch_observation/contract.json"
PARSER = "scripts/windows_git_native_branch_observation/contract.py"


def baseline_bytes(path):
    return subprocess.run(
        ["git", "show", f"{BASE}:{path}"], cwd=ROOT, check=True, capture_output=True
    ).stdout


def test_fixed_input_delta_is_exactly_last_four_identity_leaves():
    original = json.loads(baseline_bytes(METADATA))
    current = contract.read_contract()
    assert _metadata_changes(original, current) == {
        f"/source_inputs/17/{name}" for name in ("bytes", "sha256", "crlf_bytes", "crlf_sha256")
    }
    assert len(current["source_inputs"]) == 18
    assert current["base_revision"] == "5306c7134c1301dd10bee682be5ce1e61e120c46"
    assert current["source_inputs"][:17] == original["source_inputs"][:17]
    last = current["source_inputs"][-1]
    assert last["path"] == "scripts/windows_git_native_branch_observation/failure_projection.py"
    body = (ROOT / last["path"]).read_bytes()
    crlf = body.replace(b"\n", b"\r\n")
    assert last == {
        "path": last["path"],
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "crlf_bytes": len(crlf),
        "crlf_sha256": hashlib.sha256(crlf).hexdigest(),
    }


def test_parser_delta_is_only_single_f072_digest_literal():
    original = baseline_bytes(PARSER)
    old = hashlib.sha256(baseline_bytes(METADATA)).hexdigest().encode()
    new = hashlib.sha256((ROOT / METADATA).read_bytes()).hexdigest().encode()
    assert old != new and original.count(old) == 1
    assert (ROOT / PARSER).read_bytes() == original.replace(old, new)


def test_frozen_metadata_rejects_new_byte_but_original_seventeen_members_stay_exact():
    original = json.loads(baseline_bytes(METADATA))
    with pytest.raises(ValueError, match="^current_source_drift$"):
        contract.source_checks(ROOT, original)
    for row in original["source_inputs"][:17]:
        assert (ROOT / row["path"]).read_bytes() == baseline_bytes(row["path"])
    checked = contract.source_checks(ROOT, {"source_inputs": original["source_inputs"][:17]})
    assert len(checked) == 17 and all(
        row["representation"] == "EXACT_FROZEN_BYTES" for row in checked
    )
    assert len(contract.source_checks(ROOT, contract.read_contract())) == 18


def first_failure():
    observation = worker.GitMaterialFailureObservation(
        stage="git_validate", git_popen_returned=True, git_returncode=128
    )
    worker.freeze_failure_observation(observation, GitMaterialInputError("git_material_git_failed"))
    observation.stage = "git_cleanup"
    status, payload = worker.decode_failure_observation(
        worker.encode_failure_observation(observation, ValueError(CANARY))
    )
    assert status == "valid" and set(payload) == FIRST_FIELDS
    return payload


def first_record(case="A"):
    value = record(case)
    value["operations"][0]["post_worker_failure"] = first_failure()
    return value


def sibling_report(values=None):
    values = values if values is not None else [first_record(case) for case in ("A", "B")]
    sink = run_cases.CaseSink()
    for value in values:
        sink.write(frame(value))
    assert not sink.invalid and len(sink.rows) == len(sink.failure_rows) == 2
    return {
        "pytest_exit": 1,
        "invalid": False,
        "cases": sink.rows,
        failure.SIBLING_KEY: failure.failure_report(sink.failure_rows),
    }


def test_original_publish_chain_carries_first_failure_to_v2_result(tmp_path, monkeypatch):
    values = [first_record(case) for case in ("A", "B")]

    def original_pytest(arguments):
        for value in values:
            original = probe.Probe(value["selector"], "stderr-event-v1")
            original.installed_hooks = 13
            original.incomplete = True
            original.outcomes = value["outcomes"]
            original.operations = [probe.Operation(original, data=value["operations"][0])]
            assert probe._publish(original, run_cases.sys.stdout)
        return 1

    monkeypatch.setattr(pytest, "main", original_pytest)
    monkeypatch.setattr(probe.sys, "platform", "win32")
    assert run_cases.main(tmp_path) == 1
    report = json.loads((tmp_path / "cases.json").read_bytes())
    assert len(json.dumps(report).encode()) < 8192
    # 两侧均使用同一空合成分支流，避免把文件缺席与已测零计数混为一谈。
    (tmp_path / "cdb-private.log").write_text("", encoding="utf-8")
    result = observe.observation_result(tmp_path, EXECUTION)
    sibling = result["unverified_execution_observation"]["failure_observation"]
    assert sibling["schema"] == V2 and sibling["assurance"] == failure.ASSURANCE
    assert sibling["case_shape_state"] == "FINITE_AB"
    for row, source in zip(sibling["cases"], values, strict=True):
        payload = row["post_worker_failure"]
        assert row["field_states"]["post_worker_failure"] == "FINITE"
        assert payload == source["operations"][0]["post_worker_failure"]
        assert payload["origin"] == "pre_cleanup" and payload["stage"] == "git_validate"
        assert payload["error_code"] == "git_material_git_failed"
        assert payload["handler_error_code"] == "value_error"
        assert payload["git_stdout_complete"] is None and payload["git_stdout_expected"] is None
    legacy = {key: report[key] for key in failure.LEGACY_REPORT_FIELDS}
    write_report(tmp_path, legacy)
    baseline = observe.observation_result(tmp_path, EXECUTION)
    assert without_new_observation(result) == without_new_observation(baseline)
    assert not result["branch_gate_passed"]
    assert result["branch_witness"] == baseline["branch_witness"]
    assert all(
        row["proof"] == "ABSENT" and not row["original_operation_returned"]
        for row in result["cases"]
    )
    assert CANARY not in json.dumps(result)


async def test_original_guarded_decoder_reaches_same_operation_publish(tmp_path, monkeypatch):
    observation = worker.GitMaterialFailureObservation(
        stage="git_validate", git_popen_returned=True, git_returncode=128
    )
    worker.freeze_failure_observation(observation, GitMaterialInputError("git_material_git_failed"))
    finite_frame = worker.encode_failure_observation(observation, ValueError(CANARY))
    sink = run_cases.CaseSink()
    monkeypatch.setattr(probe.sys, "platform", "win32")
    for case in ("A", "B"):
        with monkeypatch.context() as patch:
            original, operation, calls = _signal_post_operation(patch, "none", finite_frame)
            # 仅合成回执字段适配；调用原字节守卫，不宣称Owner/MAC已经现场验真。
            patch.setattr(
                probe,
                "_receipt",
                lambda receipt: {
                    "provenance": "terminal_receipt_authenticated",
                    **{
                        "raw_" + name: {
                            "observed_bytes": getattr(receipt, "raw_" + name).observed_bytes,
                            "sha256": getattr(receipt, "raw_" + name).sha256,
                            "eof": getattr(receipt, "raw_" + name).eof,
                        }
                        for name in ("stdout", "stderr")
                    },
                },
            )
            original.selector = case
            original.installed_hooks = 13
            original.outcomes = {"setup": "passed", "call": "failed", "teardown": "passed"}
            await original.post_settlement()
            await original.post_settlement()
            assert calls == ["receipt", "stdout", "stderr", "raw", "raw", "protection", "signals"]
            assert operation.data["post_worker_failure_status"] == "valid"
            assert operation.data["post_worker_failure"] == first_failure()
            assert probe._publish(original, sink)
    report = {
        "pytest_exit": 1,
        "invalid": sink.invalid,
        "cases": sink.rows,
        failure.SIBLING_KEY: failure.failure_report(sink.failure_rows),
    }
    write_report(tmp_path, report)
    result = observe.observation_result(tmp_path, EXECUTION)
    assert result["unverified_execution_observation"]["failure_observation"]["schema"] == V2
    assert all(
        row["post_worker_failure"] == first_failure()
        for row in result["unverified_execution_observation"]["failure_observation"]["cases"]
    )
    assert not result["branch_gate_passed"]


def test_validator_is_original_and_rejects_scalar_and_dict_subclasses():
    assert failure.worker_failure_valid is worker._valid

    class IntLike(int):
        pass

    class DictLike(dict):
        pass

    payload = first_failure()
    assert not failure._field_valid("post_worker_failure", DictLike(payload))
    payload["git_returncode"] = IntLike(128)
    assert not failure._field_valid("post_worker_failure", payload)


@pytest.mark.parametrize("field", ["post_worker_failure", "handler_error_code"])
def test_first_failure_duplicate_keys_keep_original_sink_invalid(field):
    text = frame(first_record()).replace(f'"{field}":', f'"{field}": null, "{field}":', 1)
    sink = run_cases.CaseSink()
    sink.write(text)
    assert sink.invalid and not sink.rows and not sink.failure_rows


@pytest.mark.parametrize("field", sorted(FIRST_FIELDS))
@pytest.mark.parametrize("damage", ["missing", "wrongtype"])
def test_each_first_failure_key_is_exact(field, damage):
    value = first_record()
    payload = value["operations"][0]["post_worker_failure"]
    if damage == "missing":
        del payload[field]
    else:
        payload[field] = [CANARY]
    row = failure.project_failure_case(frame(value))
    assert row["field_states"]["post_worker_failure"] == "REJECTED"
    assert row["post_worker_failure"] is None and CANARY not in json.dumps(row)


@pytest.mark.parametrize("field", ["msg", "path", "argv", "PID"])
def test_extra_first_failure_fields_are_not_filtered_into_acceptance(field):
    value = first_record()
    value["operations"][0]["post_worker_failure"][field] = CANARY
    row = failure.project_failure_case(frame(value))
    assert row["field_states"]["post_worker_failure"] == "REJECTED"
    assert row["post_worker_failure"] is None and CANARY not in json.dumps(row)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": V2},
        {"stage": "git_validate_suffix"},
        {"stage": True},
        {"origin": "cleanup"},
        {"error_code": "git_material_git_failed_suffix"},
        {"handler_error_code": "arbitrary"},
        {"origin": "handler"},
        {"git_returncode": True},
        {"git_returncode": False},
        {"git_returncode": 128.0},
        {"git_returncode": -(2**31) - 1},
        {"git_returncode": 2**32},
        {"git_popen_returned": 1},
        {"git_stdout_complete": 0},
        {"git_stdout_expected": 1},
        {"git_popen_returned": False},
        {"git_returncode": None, "git_stdout_complete": False},
        {"git_stdout_expected": False},
        {"git_stdout_complete": True, "git_stdout_expected": True},
    ],
)
def test_worker_types_bounds_codes_and_consistency_are_reused(changes):
    value = first_record()
    value["operations"][0]["post_worker_failure"].update(changes)
    assert not worker._valid(value["operations"][0]["post_worker_failure"])
    row = failure.project_failure_case(frame(value))
    assert row["field_states"]["post_worker_failure"] == "REJECTED"
    assert row["post_worker_failure"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"git_returncode": -(2**31)},
        {"git_returncode": 2**32 - 1},
        {"git_stdout_complete": False},
        {"git_stdout_complete": True},
        {"git_returncode": 0, "git_stdout_complete": True, "git_stdout_expected": False},
        {"git_returncode": 0, "git_stdout_complete": True, "git_stdout_expected": True},
        {"git_popen_returned": False, "git_returncode": None},
        {"origin": "handler", "handler_error_code": "git_material_git_failed"},
        {"stage": "unobserved", "error_code": "unclassified", "handler_error_code": "unclassified"},
    ],
)
def test_original_none_false_unknown_and_boundary_values_survive(changes):
    values = [first_record(case) for case in ("A", "B")]
    for value in values:
        value["operations"][0]["post_worker_failure"].update(changes)
    report = sibling_report(values)
    _, sibling = failure.isolate_failure_report(report)
    assert sibling["schema"] == V2 and sibling["case_shape_state"] == "FINITE_AB"
    assert (
        sibling["cases"][0]["post_worker_failure"]
        == values[0]["operations"][0]["post_worker_failure"]
    )


@pytest.mark.parametrize("status", ["invalid", "not_observed", None, True, "unknown"])
def test_source_status_mismatch_rejects_only_first_payload(status):
    value = first_record()
    value["operations"][0]["post_worker_failure_status"] = status
    row = failure.project_failure_case(frame(value))
    assert row["post_worker_failure"] is None
    assert row["field_states"]["post_worker_failure"] == "REJECTED"
    if status in {"invalid", "not_observed"}:
        assert row["post_worker_failure_status"] == status


@pytest.mark.parametrize("code", [0, None, True, 128.0])
def test_source_original_case_returncode_mismatch_fails_closed(code):
    value = first_record()
    value["operations"][0]["post_proof"]["git_returncode"] = code
    baseline = projection.project_case(frame(value))
    row = failure.project_failure_case(frame(value))
    assert row["post_worker_failure"] is None
    assert row["field_states"]["post_worker_failure"] == "REJECTED"
    assert projection.project_case(frame(value)) == baseline


@pytest.mark.parametrize("damage", ["eof", "sha", "truncated"])
def test_first_payload_cannot_bypass_original_raw_facts(damage):
    value = first_record()
    if damage == "eof":
        value["operations"][0]["post_raw"]["stderr"]["eof"] = False
    elif damage == "sha":
        value["operations"][0]["post_receipt"]["raw_stderr"]["sha256"] = "b" * 64
    else:
        value["diagnostic_truncated"] = True
    row = failure.project_failure_case(frame(value))
    assert row["field_states"]["post_worker_failure"] == "REJECTED"
    assert row["post_worker_failure"] is None


@pytest.mark.parametrize("payload", [None, True, [], {}, {"git_returncode": True}])
def test_malformed_new_candidate_does_not_downgrade_to_legacy(payload):
    value = first_record()
    value["operations"][0]["post_worker_failure"] = payload
    row = failure.project_failure_case(frame(value))
    assert set(row) == {"case", "field_states", *failure.FAILURE_FIELDS_V2}
    assert row["post_worker_failure"] is None
    assert row["field_states"]["post_worker_failure"] == "REJECTED"


def test_explicit_v2_absence_is_unknown_and_mixed_builder_does_not_invent_payload():
    value = record()
    del value["operations"][0]["post_worker_failure"]
    row = failure.project_failure_case(frame(value), schema=V2)
    assert row["post_worker_failure"] is None
    assert row["field_states"]["post_worker_failure"] == "NOT_AVAILABLE"
    report = sibling_report([record("A"), first_record("B")])
    _, sibling = failure.isolate_failure_report(report)
    assert sibling["schema"] == V2 and sibling["case_shape_state"] == "FINITE_AB"
    assert sibling["cases"][0]["post_worker_failure"] is None
    assert sibling["cases"][0]["field_states"]["post_worker_failure"] == "NOT_AVAILABLE"
    assert sibling["cases"][1]["field_states"]["post_worker_failure"] == "FINITE"


def test_frozen_v1_reader_and_single_returncode_publisher_are_exact():
    source = subprocess.run(
        [
            "git",
            "show",
            f"{BASE}:scripts/windows_git_native_branch_observation/failure_projection.py",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    namespace = {"__name__": "frozen_failure_projection"}
    exec(compile(source, "frozen_failure_projection", "exec"), namespace)
    values = [record(case) for case in ("A", "B")]
    old_rows = [namespace["project_failure_case"](frame(value)) for value in values]
    assert [failure.project_failure_case(frame(value)) for value in values] == old_rows
    report = sibling_report(values)
    expected = namespace["isolate_failure_report"](copy.deepcopy(report))
    assert failure.isolate_failure_report(report) == expected
    assert failure.failure_report(old_rows) == namespace["failure_report"](old_rows)
    full = first_record()
    assert failure.project_failure_case(frame(full), schema=failure.SCHEMA) == namespace[
        "project_failure_case"
    ](frame(full))


@pytest.mark.parametrize("version", [failure.SCHEMA, V2])
def test_v1_v2_label_and_shape_cannot_be_swapped(version):
    report = (
        sibling_report([record(case) for case in ("A", "B")]) if version == V2 else sibling_report()
    )
    report[failure.SIBLING_KEY]["schema"] = version
    legacy, sibling = failure.isolate_failure_report(report)
    assert legacy["cases"] == report["cases"]
    assert sibling["case_shape_state"] == "REJECTED" and not sibling["cases"]


@pytest.mark.parametrize(
    "damage",
    ["status", "status_state", "code", "raw", "truncated", "extra", "missing", "bool", "origin"],
)
def test_read_side_forged_first_failure_rejects_sibling_without_changing_gate(tmp_path, damage):
    report = sibling_report()
    row = report[failure.SIBLING_KEY]["cases"][0]
    payload = row["post_worker_failure"]
    if damage == "status":
        row["post_worker_failure_status"] = "invalid"
    elif damage == "status_state":
        row["field_states"]["post_worker_failure_status"] = "NOT_AVAILABLE"
        row["post_worker_failure_status"] = None
    elif damage == "code":
        payload["git_returncode"] = 0
    elif damage == "raw":
        report["cases"][0]["raw_validation"] = "UNAVAILABLE"
    elif damage == "truncated":
        report["cases"][0]["diagnostic_truncated"] = True
    elif damage == "extra":
        payload["msg"] = CANARY
    elif damage == "missing":
        del payload["stage"]
    elif damage == "bool":
        payload["git_returncode"] = True
    else:
        payload["origin"] = "handler"
    legacy = {key: report[key] for key in failure.LEGACY_REPORT_FIELDS}
    write_report(tmp_path, legacy)
    baseline = observe.observation_result(tmp_path, EXECUTION)
    write_report(tmp_path, report)
    result = observe.observation_result(tmp_path, EXECUTION)
    sibling = result["unverified_execution_observation"]["failure_observation"]
    assert sibling["case_shape_state"] == "REJECTED" and sibling["cases"] == []
    assert without_new_observation(result) == without_new_observation(baseline)
    assert CANARY not in json.dumps(result)


def test_all_mutable_layers_are_copied_at_builder_and_reader():
    source = first_record()
    row = failure.project_failure_case(frame(source))
    source["operations"][0]["post_worker_failure"]["stage"] = CANARY
    assert row["post_worker_failure"]["stage"] == "git_validate"
    rows = [row, failure.project_failure_case(frame(first_record("B")))]
    built = failure.failure_report(rows)
    rows[0]["post_worker_failure"]["stage"] = CANARY
    rows[0]["field_states"]["post_worker_failure"] = CANARY
    rows[0]["post_git_trace2"]["stage_witnesses"].append(CANARY)
    assert CANARY not in json.dumps(built)
    report = sibling_report()
    report[failure.SIBLING_KEY] = built
    _, isolated = failure.isolate_failure_report(report)
    built["cases"][0]["post_worker_failure"]["stage"] = CANARY
    built["cases"][0]["post_stderr_signals"]["HASH_FD"] = CANARY
    assert CANARY not in json.dumps(isolated)


def test_invalid_version_is_not_a_permissive_fallback():
    with pytest.raises(ValueError, match="failure_schema_invalid"):
        failure.project_failure_case(frame(first_record()), schema="v3")
