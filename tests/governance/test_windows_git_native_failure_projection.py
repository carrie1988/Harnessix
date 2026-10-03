"""有限失败Sibling的真实发布接缝、精确负例及原结果差分；不执行原生Run。"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.delivery.git_material_trace2_profile import TRACE2_ERROR_FORMATS
from scripts.windows_git_native_branch_observation import failure_projection as failure
from scripts.windows_git_native_branch_observation import observe, projection, run_cases
from tests.governance.test_git_trace2_projection import _events, _project, _wire
from tests.governance.test_windows_git_native_branch_observation import probe_record, witness
from tests.product_config import git_minimum_commit_probe as probe
from tests.product_config.git_stderr_signals import _STDERR_LITERALS, _stderr_signals

ROOT = Path(__file__).resolve().parents[2]
BASE = "b8123324908d1a27dee9a49dd52a0a23de49e6a3"
CANARY = "不得发布正文-path-PID-errno-MAC"
EXECUTION = {"debugger_exit": 1, "timed_out": False, "log_limit_stopped": False}


def record(case="A"):
    value = probe_record(case)
    value["operations"][0].update(
        post_stderr_signals=_stderr_signals(b"git_material_worker_failed\n"),
        post_worker_failure_status="valid",
        post_git_trace2=_project(_wire(*_events(128)), git_returncode=128),
    )
    return value


def frame(value):
    return projection.PROBE_PREFIX + json.dumps(value, ensure_ascii=True) + "\n"


def publish_real(case, sink, monkeypatch):
    value = record(case)
    original = probe.Probe(case, "stderr-event-v1")
    original.installed_hooks = 13
    original.incomplete = True
    original.outcomes = value["outcomes"]
    original.operations = [probe.Operation(original, data=value["operations"][0])]
    with monkeypatch.context() as patch:
        patch.setattr(probe.sys, "platform", "win32")
        assert probe._publish(original, sink)
    return original


def report_with_sibling():
    values = [record(case) for case in ("A", "B")]
    return {
        "pytest_exit": 1,
        "invalid": False,
        "cases": [projection.project_case(frame(value)) for value in values],
        failure.SIBLING_KEY: failure.failure_report(
            [failure.project_failure_case(frame(value)) for value in values]
        ),
    }


def write_report(tmp_path, report, log=""):
    (tmp_path / "cases.json").write_text(json.dumps(report), encoding="utf-8")
    (tmp_path / "cdb-private.log").write_text(log, encoding="utf-8")


@pytest.fixture(scope="module")
def original_observer():
    # 从固定Git对象内存加载，不读取其他工作树，不复制或改写冻结验证包。
    source = subprocess.run(
        ["git", "show", f"{BASE}:scripts/windows_git_native_branch_observation/observe.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    namespace = {"__name__": "baseline_observe"}
    exec(compile(source, "baseline_observe", "exec"), namespace)
    return SimpleNamespace(**namespace)


def without_new_observation(result):
    result = copy.deepcopy(result)
    result["unverified_execution_observation"].pop("failure_observation")
    return result


def test_actual_original_publish_reaches_run_cases_and_finite_result(tmp_path, monkeypatch):
    seen = []

    def original_pytest(arguments):
        import sys

        seen.extend(arguments)
        for case in ("A", "B"):
            sys.stdout.write("F")
            publish_real(case, sys.stdout, monkeypatch)
        return 1

    monkeypatch.setattr(pytest, "main", original_pytest)
    assert run_cases.main(tmp_path) == 1
    report = json.loads((tmp_path / "cases.json").read_bytes())
    assert set(report) == failure.LEGACY_REPORT_FIELDS | {failure.SIBLING_KEY}
    assert report["invalid"] is False
    assert seen[-2:] == run_cases.read_contract()["selectors"]
    assert "--git-material-trace2=stderr-event-v1" in seen
    assert "tests.product_config.git_minimum_commit_probe" in seen
    assert set(report["cases"][0]) == projection.CASE_FIELDS
    assert len(json.dumps(report).encode()) < 8192
    result = observe.observation_result(tmp_path, EXECUTION)
    sibling = result["unverified_execution_observation"]["failure_observation"]
    assert sibling["schema"] == failure.SCHEMA and sibling["assurance"] == failure.ASSURANCE
    assert sibling["case_shape_state"] == "FINITE_AB"
    assert not result["branch_gate_passed"]
    for row in sibling["cases"]:
        assert set(row["post_stderr_signals"]) == set(failure.SIGNAL_FIELDS)
        assert row["post_stderr_signals"]["worker_failure_literal"] is True
        assert row["post_worker_failure_status"] == "valid"
        assert row["post_git_trace2"]["return_consistency"] == "MATCHED_128"
        assert set(row["field_states"].values()) == {"FINITE"}
    assert CANARY not in json.dumps(result)


def test_catalog_matches_original_nine_signals_and_trace2_projection():
    assert tuple(_STDERR_LITERALS) == failure.SIGNAL_FIELDS
    assert set(failure.FORMAT_IDS) == {row.fixed_id for row in TRACE2_ERROR_FORMATS.values()}
    for value in (
        _project(_wire(*_events(0)), git_returncode=0),
        _project(_wire(*_events(128)), git_returncode=128),
        _project(b"opaque-unknown\n"),
        _project(b"opaque"),
        _project(b""),
    ):
        assert failure._trace_valid(value)


def test_original_catalog_selector_detects_validator_regression(monkeypatch):
    # 原选择器必须独立识别有限发布校验器退化，不能依赖新负例顺带覆盖。
    monkeypatch.setattr(failure, "_trace_valid", lambda value: False)
    with pytest.raises(AssertionError):
        test_catalog_matches_original_nine_signals_and_trace2_projection()


@pytest.mark.parametrize(
    "fixed_id",
    ["OBJECT_INDEX_SHORT_READ", "OBJECT_DATABASE_ADD_PERMISSION", "OBJECT_FINALIZE_PERMISSION"],
)
def test_static_enum_reaches_finite_sibling_without_changing_original_gate(
    tmp_path, fixed_id, original_observer
):
    report = report_with_sibling()
    for row in report[failure.SIBLING_KEY]["cases"]:
        row["post_git_trace2"]["error_format_ids"] = [fixed_id]
    legacy = {key: report[key] for key in failure.LEGACY_REPORT_FIELDS}
    write_report(tmp_path, legacy)
    original = original_observer.observation_result(tmp_path, EXECUTION)
    write_report(tmp_path, report)
    result = observe.observation_result(tmp_path, EXECUTION)
    assert without_new_observation(result) == original
    sibling = result["unverified_execution_observation"]["failure_observation"]
    assert sibling["case_shape_state"] == "FINITE_AB"
    assert all(row["post_git_trace2"]["error_format_ids"] == [fixed_id] for row in sibling["cases"])
    assert not result["branch_gate_passed"]


@pytest.mark.parametrize(
    "values",
    [
        ["OBJECT_FINALIZE_PERMISSION_SUFFIX"],
        ["OBJECT_FINALIZE_PERMISSION", "OBJECT_FINALIZE_PERMISSION"],
        ["OBJECT_INDEX_SHORT_READ", "OBJECT_DATABASE_ADD_PERMISSION"],
        [True],
    ],
)
def test_static_sibling_rejects_unknown_duplicate_unsorted_or_nonstring_ids(values):
    report = report_with_sibling()
    report[failure.SIBLING_KEY]["cases"][0]["post_git_trace2"]["error_format_ids"] = values
    _, sibling = failure.isolate_failure_report(report)
    assert sibling["case_shape_state"] == "REJECTED"


@pytest.mark.parametrize("key", failure.FAILURE_FIELDS)
@pytest.mark.parametrize("value", [None, True, 1, [], "opaque", {"body": CANARY}])
def test_wrongtype_is_isolated_without_changing_original_case(key, value):
    original = record()
    baseline = projection.project_case(frame(original))
    original["operations"][0][key] = value
    assert projection.project_case(frame(original)) == baseline
    row = failure.project_failure_case(frame(original))
    assert row["field_states"][key] == "REJECTED" and row[key] is None
    assert CANARY not in json.dumps(row)


@pytest.mark.parametrize("key", failure.FAILURE_FIELDS)
def test_missing_is_not_false_or_not_observed(key):
    value = record()
    del value["operations"][0][key]
    row = failure.project_failure_case(frame(value))
    assert row["field_states"][key] == "NOT_AVAILABLE" and row[key] is None
    assert all(
        row["field_states"][other] == "FINITE" for other in failure.FAILURE_FIELDS if other != key
    )


@pytest.mark.parametrize("status", ["not_observed", "invalid", "valid"])
def test_original_worker_failure_states_are_observations_not_authentication(status):
    value = record()
    value["operations"][0]["post_worker_failure_status"] = status
    row = failure.project_failure_case(frame(value))
    assert row["post_worker_failure_status"] == status
    assert row["field_states"]["post_worker_failure_status"] == "FINITE"


@pytest.mark.parametrize("key", failure.SIGNAL_FIELDS)
@pytest.mark.parametrize("mode", ["missing", "wrongtype", "extra"])
def test_exact_nine_bool_contract(key, mode):
    value = record()
    signals = value["operations"][0]["post_stderr_signals"]
    if mode == "missing":
        del signals[key]
    elif mode == "wrongtype":
        signals[key] = 1
    else:
        signals[CANARY] = True
    row = failure.project_failure_case(frame(value))
    assert row["field_states"]["post_stderr_signals"] == "REJECTED"
    assert row["post_stderr_signals"] is None and CANARY not in json.dumps(row)


@pytest.mark.parametrize("key", [*failure.TRACE_FIELDS, "stage_witnesses", "error_format_ids"])
@pytest.mark.parametrize("mode", ["missing", "wrongtype", "extra"])
def test_exact_seven_trace2_fields(key, mode):
    value = record()
    trace = value["operations"][0]["post_git_trace2"]
    if mode == "missing":
        del trace[key]
    elif mode == "wrongtype":
        trace[key] = {"body": CANARY}
    else:
        trace["pid"] = CANARY
    row = failure.project_failure_case(frame(value))
    assert row["field_states"]["post_git_trace2"] == "REJECTED"
    assert row["post_git_trace2"] is None and CANARY not in json.dumps(row)


@pytest.mark.parametrize(
    "key,value",
    [
        ("stage_witnesses", ["ENTRY_START_MATCHED"] * 2),
        ("stage_witnesses", list(reversed(failure.WITNESSES))),
        ("stage_witnesses", ["ENTRY_START_MATCHED"] * 4),
        ("error_format_ids", ["HASH_OBJECT_ADD_AGGREGATE"] * 2),
        ("error_format_ids", ["HASH_OBJECT_ADD_AGGREGATE"] * 7),
        ("error_format_ids", [CANARY]),
        ("reason", CANARY),
        ("source_profile_id", CANARY),
    ],
)
def test_trace2_duplicate_overlimit_order_and_unknown_enums_rejected(key, value):
    original = record()
    original["operations"][0]["post_git_trace2"][key] = value
    row = failure.project_failure_case(frame(original))
    assert row["post_git_trace2"] is None
    assert row["field_states"]["post_git_trace2"] == "REJECTED"


@pytest.mark.parametrize("key", failure.FAILURE_FIELDS)
def test_duplicate_json_keys_keep_original_sink_invalid(key):
    text = frame(record()).replace(f'"{key}":', f'"{key}": null, "{key}":', 1)
    sink = run_cases.CaseSink()
    sink.write(text)
    assert sink.invalid and not sink.rows and not sink.failure_rows


@pytest.mark.parametrize("cut", [1, 5, len(projection.PROBE_PREFIX), -2])
def test_real_render_split_and_pending_write_boundaries(cut, monkeypatch):
    stream = io.StringIO()
    publish_real("A", stream, monkeypatch)
    text = stream.getvalue()
    sink = run_cases.CaseSink()
    sink.write(text[:cut])
    sink.write(text[cut:])
    assert not sink.invalid and len(sink.rows) == len(sink.failure_rows) == 1


@pytest.mark.parametrize(
    "mode",
    [
        "same_write_noise",
        "partial_prefix",
        "overlimit",
        "extra",
        "pending_limit",
        "utf8_limit",
        "eof",
    ],
)
def test_original_case_sink_boundary_and_limits_are_not_relaxed(mode, monkeypatch):
    stream = io.StringIO()
    publish_real("A", stream, monkeypatch)
    text = stream.getvalue()
    sink = run_cases.CaseSink()
    if mode == "same_write_noise":
        sink.write("F" + text)
        assert not sink.rows and not sink.failure_rows
    elif mode == "partial_prefix":
        sink.write(projection.PROBE_PREFIX[:5])
        sink.write(text)
        assert sink.invalid and not sink.rows and not sink.failure_rows
    elif mode == "overlimit":
        sink.write("x" * 65537)
        assert sink.invalid and not sink.rows and not sink.failure_rows
    elif mode == "pending_limit":
        sink.write("x" * 40000)
        sink.write("x" * 40000)
        assert sink.invalid and not sink.pending
    elif mode == "utf8_limit":
        value = record()
        value["padding"] = "汉" * 24000
        text = projection.PROBE_PREFIX + json.dumps(value, ensure_ascii=False) + "\n"
        assert len(text) < 65536 < len(text.encode())
        sink.write(text)
        assert sink.invalid and not sink.rows and not sink.failure_rows
    elif mode == "eof":
        sink.write(text[:-1])
        assert sink.pending.startswith(projection.PROBE_PREFIX)
        assert not sink.rows and not sink.failure_rows
    else:
        for case in ("A", "B", "A"):
            publish_real(case, sink, monkeypatch)
        assert sink.invalid and len(sink.rows) == len(sink.failure_rows) == 2


@pytest.mark.parametrize(
    "damage",
    [
        "schema",
        "assurance",
        "missing",
        "extra",
        "wrongtype",
        "duplicate_case",
        "overlimit",
        "row_extra",
        "state",
        "payload",
        "trace_extra",
        "report_extra",
    ],
)
def test_malformed_sibling_isolated_from_old_gate_and_no_sensitive_publication(
    tmp_path, damage, original_observer
):
    report = report_with_sibling()
    sibling = report[failure.SIBLING_KEY]
    if damage in {"schema", "assurance"}:
        sibling[damage] = CANARY
    elif damage == "missing":
        del sibling["cases"]
    elif damage == "extra":
        sibling["body"] = CANARY
    elif damage == "wrongtype":
        report[failure.SIBLING_KEY] = [CANARY]
    elif damage == "duplicate_case":
        sibling["cases"][1]["case"] = "A"
    elif damage == "overlimit":
        sibling["cases"].append(sibling["cases"][0])
    elif damage == "row_extra":
        sibling["cases"][0]["path"] = CANARY
    elif damage == "state":
        sibling["cases"][0]["field_states"]["post_stderr_signals"] = CANARY
    elif damage == "payload":
        sibling["cases"][0]["post_stderr_signals"] = {"body": CANARY}
    elif damage == "trace_extra":
        sibling["cases"][0]["post_git_trace2"]["errno"] = CANARY
    else:
        report["body"] = CANARY
    legacy = {key: report[key] for key in failure.LEGACY_REPORT_FIELDS}
    if damage == "report_extra":
        legacy["body"] = CANARY
    log = witness([(1, "FSTAT", -1), (2, "FSTAT", -1)])
    write_report(tmp_path, legacy, log)
    old = original_observer.observation_result(tmp_path, EXECUTION)
    write_report(tmp_path, report, log)
    result = observe.observation_result(tmp_path, EXECUTION)
    assert without_new_observation(result) == old
    assert (
        result["unverified_execution_observation"]["failure_observation"]["case_shape_state"]
        == "REJECTED"
    )
    assert CANARY not in json.dumps(result)


@pytest.mark.parametrize("log_mode", ["zero", "arm", "valid", "bad"])
@pytest.mark.parametrize(
    "case_mode", ["valid", "raw_missing", "truncated", "invalid", "exit5", "duplicate", "empty"]
)
@pytest.mark.parametrize(
    "execution_mode", ["normal", "timeout", "limited", "bad_exit", "bool_exit"]
)
def test_all_old_results_and_complete_gate_differential(
    tmp_path, log_mode, case_mode, execution_mode, original_observer
):
    report = report_with_sibling()
    sibling = report.pop(failure.SIBLING_KEY)
    if case_mode == "raw_missing":
        report["cases"][0]["raw_validation"] = "UNAVAILABLE"
    elif case_mode == "truncated":
        report["cases"][0]["diagnostic_truncated"] = True
    elif case_mode == "invalid":
        report["invalid"] = True
    elif case_mode == "exit5":
        report["pytest_exit"] = 5
    elif case_mode == "duplicate":
        report["cases"][1]["case"] = "A"
    elif case_mode == "empty":
        report["cases"] = []
    logs = {
        "zero": "",
        "arm": "FHX_NATIVE_ARM pid=1\nFHX_NATIVE_ARM pid=2\n",
        "valid": witness([(1, "FSTAT", -1), (2, "FSTAT", -1)]),
        "bad": "FHX_NATIVE_BRANCH opaque\n",
    }
    execution = dict(EXECUTION)
    if execution_mode == "timeout":
        execution["timed_out"] = True
    elif execution_mode == "limited":
        execution["log_limit_stopped"] = True
    elif execution_mode == "bad_exit":
        execution["debugger_exit"] = 3
    elif execution_mode == "bool_exit":
        execution["debugger_exit"] = True
    write_report(tmp_path, report, logs[log_mode])
    old = original_observer.observation_result(tmp_path, execution)
    assert without_new_observation(observe.observation_result(tmp_path, execution)) == old
    report[failure.SIBLING_KEY] = sibling
    write_report(tmp_path, report, logs[log_mode])
    assert without_new_observation(observe.observation_result(tmp_path, execution)) == old


def test_case_report_overlimit_is_not_read(tmp_path):
    report = report_with_sibling()
    report["padding"] = CANARY * 1000
    write_report(tmp_path, report)
    result = observe.observation_result(tmp_path, EXECUTION)
    assert result["cases"] == [] and result["pytest_exit"] is None
    assert (
        result["unverified_execution_observation"]["failure_observation"]["case_shape_state"]
        == "NOT_AVAILABLE"
    )
    assert not result["branch_gate_passed"] and CANARY not in json.dumps(result)


@pytest.mark.parametrize("key", failure.FAILURE_FIELDS)
@pytest.mark.parametrize("mode", ["missing", "wrongtype"])
def test_rejected_field_survives_real_sink_as_no_payload(tmp_path, key, mode):
    sink = run_cases.CaseSink()
    for case in ("A", "B"):
        value = record(case)
        if mode == "missing":
            del value["operations"][0][key]
        else:
            value["operations"][0][key] = {"body": CANARY}
        sink.write("F")
        sink.write(frame(value))
    assert not sink.invalid and len(sink.rows) == len(sink.failure_rows) == 2
    write_report(
        tmp_path,
        {
            "cases": sink.rows,
            "pytest_exit": 1,
            "invalid": sink.invalid,
            failure.SIBLING_KEY: failure.failure_report(sink.failure_rows),
        },
    )
    result = observe.observation_result(tmp_path, EXECUTION)
    sibling = result["unverified_execution_observation"]["failure_observation"]
    assert sibling["case_shape_state"] == "FINITE_AB"
    expected = "NOT_AVAILABLE" if mode == "missing" else "REJECTED"
    assert all(
        row["field_states"][key] == expected and row[key] is None for row in sibling["cases"]
    )
    assert CANARY not in json.dumps(result)


@pytest.mark.parametrize("damage", ["duplicate", "utf8", "top_list", "unhashable"])
def test_original_result_exception_types_unchanged(tmp_path, damage, original_observer):
    report = report_with_sibling()
    del report[failure.SIBLING_KEY]
    body = json.dumps(report).encode()
    expected = ValueError
    if damage == "duplicate":
        body = body.replace(b'"cases":', b'"cases": null, "cases":', 1)
    elif damage == "utf8":
        body = b"\xff"
        expected = UnicodeDecodeError
    elif damage == "top_list":
        body = b"[]"
    else:
        report["cases"][0]["case"] = []
        body = json.dumps(report).encode()
        expected = TypeError
    (tmp_path / "cases.json").write_bytes(body)
    for observer in (original_observer, observe):
        with pytest.raises(expected) as error:
            observer.observation_result(tmp_path, EXECUTION)
        assert type(error.value) is expected


def test_sibling_empty_cannot_replace_available_cases():
    report = report_with_sibling()
    report[failure.SIBLING_KEY] = failure.failure_observation(state="EMPTY")
    legacy, sibling = failure.isolate_failure_report(report)
    assert legacy["cases"] == report["cases"]
    assert sibling["case_shape_state"] == "REJECTED"


def test_finite_sibling_rebuilds_without_aliases():
    report = report_with_sibling()
    _, sibling = failure.isolate_failure_report(report)
    report[failure.SIBLING_KEY]["cases"][0]["post_git_trace2"]["stage_witnesses"].append(CANARY)
    report[failure.SIBLING_KEY]["cases"][0]["post_stderr_signals"]["HASH_FD"] = CANARY
    assert CANARY not in json.dumps(sibling)


@pytest.mark.parametrize("key", ["case", "field_states", *failure.FAILURE_FIELDS])
@pytest.mark.parametrize("mode", ["missing", "wrongtype", "extra"])
def test_every_sibling_row_key_has_closed_negative_contract(key, mode):
    report = report_with_sibling()
    row = report[failure.SIBLING_KEY]["cases"][0]
    if mode == "missing":
        del row[key]
    elif mode == "wrongtype":
        row[key] = [CANARY]
    else:
        row["opaque"] = CANARY
    legacy, sibling = failure.isolate_failure_report(report)
    assert legacy["cases"] == report["cases"]
    assert sibling["case_shape_state"] == "REJECTED"
    assert CANARY not in json.dumps(sibling)


@pytest.mark.parametrize("state", ["EMPTY", "REJECTED", "FINITE_AB"])
@pytest.mark.parametrize(
    "key,value", [("pytest_exit", True), ("pytest_exit", 6), ("invalid", 0), ("cases", {})]
)
def test_sibling_cannot_bypass_legacy_report_types(state, key, value):
    report = report_with_sibling()
    report[failure.SIBLING_KEY]["case_shape_state"] = state
    if state != "FINITE_AB":
        report[failure.SIBLING_KEY]["cases"] = []
    report[key] = value
    _, sibling = failure.isolate_failure_report(report)
    assert sibling["case_shape_state"] == "REJECTED"


def test_legacy_case_fields_and_full_gate_sources_are_unchanged():
    contract = run_cases.read_contract()
    assert len(contract["source_inputs"]) == 18
    assert contract["budgets"] == {
        "command_seconds": 20,
        "operation_seconds": 45,
        "workflow_step_seconds": 300,
        "outer_watchdog_seconds": 240,
    }
    for path in (
        "scripts/windows_git_native_branch_observation/projection.py",
        "scripts/windows_git_native_branch_observation/preflight.py",
        "scripts/windows_git_native_branch_observation/identity.py",
        "scripts/windows_git_native_branch_observation/diagnostics.py",
        "tests/product_config/git_stderr_signals.py",
    ):
        original = subprocess.run(
            ["git", "show", f"{BASE}:{path}"], cwd=ROOT, check=True, capture_output=True
        ).stdout
        assert (ROOT / path).read_bytes() == original

    # 已评审变更改用新的整体字节锚点，不能删除冻结门禁或按语义自动放行。
    approved = {
        "scripts/windows_git_native_branch_observation/contract.py": (
            3937,
            "8d2d2ec908745e94be187834ea271251998714fd3a340e977eb8b2c5ae7e55db",
        ),
        "scripts/windows_git_native_branch_observation/contract.json": (
            8293,
            "0a5668e04c462ffab13f41592bd79ba22eb9acfaa1703d12e7d08b59e70f6859",
        ),
        "tests/product_config/git_minimum_commit_probe.py": (
            24603,
            "f348ddf2d4490870c26511856eabf92dc079494902e679d3b3e04c0d1f56e2fb",
        ),
        "tests/product_config/git_trace2_projection.py": (
            14677,
            "7cf2410e5b75dc197ad92f573751ed181ab5c4f9c808ce6354b88f44f307d7f4",
        ),
    }
    for path, expected in approved.items():
        body = (ROOT / path).read_bytes()
        assert (len(body), hashlib.sha256(body).hexdigest()) == expected
