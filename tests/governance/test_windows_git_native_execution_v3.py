"""v3有限观察与原认证门分离；不执行Windows、SDK或模型。"""

import importlib.util
import json
from pathlib import Path

import pytest

from scripts.windows_git_native_branch_observation import observe, projection
from scripts.windows_git_native_branch_observation.projection import PROBE_PREFIX
from scripts.windows_git_native_branch_observation.run_cases import CaseSink
from tests.governance.test_windows_git_native_branch_observation import probe_record, witness

ROOT = Path(__file__).resolve().parents[2]


def finite_rows():
    return [projection.project_case(frame(case).rstrip("\n")) for case in ("A", "B")]


def write_report(path, rows, invalid=False, **extra):
    value = {"pytest_exit": 1, "cases": rows, "invalid": invalid, **extra}
    (path / "cases.json").write_text(json.dumps(value))
    (path / "cdb-private.log").write_text(witness([(1, "FSTAT", -1), (2, "FSTAT", -1)]))


def execution():
    return {"debugger_exit": 1, "timed_out": False, "log_limit_stopped": False}


@pytest.fixture
def old_observe():
    path = ROOT / "docs/validation/git-native-execution-envelope-2026-10-02-v3/originals/observe.py"
    spec = importlib.util.spec_from_file_location("frozen_observer_v2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def old_projection():
    path = (
        ROOT / "docs/validation/git-native-execution-envelope-2026-10-02-v3/originals/projection.py"
    )
    spec = importlib.util.spec_from_file_location("frozen_projection_v2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frame(case="A"):
    return PROBE_PREFIX + json.dumps(probe_record(case), separators=(",", ":")) + "\n"


def test_complete_publication_after_pending_pytest_progress():
    sink = CaseSink()
    for case in ("A", "B"):
        sink.write("F")
        sink.write(frame(case))
    assert [x["case"] for x in sink.rows] == ["A", "B"]
    assert not sink.invalid


@pytest.mark.parametrize(
    "cut", [1, 5, len(PROBE_PREFIX) - 1, len(PROBE_PREFIX), len(PROBE_PREFIX) + 1, -2]
)
def test_split_valid_frame_retains_original_behavior(cut):
    sink = CaseSink()
    text = frame()
    sink.write(text[:cut])
    sink.write(text[cut:])
    assert len(sink.rows) == 1 and not sink.invalid


@pytest.mark.parametrize("pending", [PROBE_PREFIX, PROBE_PREFIX + "{", PROBE_PREFIX[:5]])
def test_existing_unfinished_protocol_is_not_discarded_as_noise(pending):
    sink = CaseSink()
    sink.write(pending)
    sink.write(frame())
    assert not sink.rows
    # 候选对完整或局部未完prefix冻结invalid，不能冒充正常噪声。
    assert sink.invalid


@pytest.mark.parametrize(
    "text",
    [
        PROBE_PREFIX + "{bad-json}\n",
        PROBE_PREFIX + '{"schema":"x","schema":"y"}\n',
        PROBE_PREFIX + "[]\n",
        PROBE_PREFIX + "{}\n",
    ],
)
def test_malformed_frames_still_invalid(text):
    sink = CaseSink()
    sink.write(text)
    assert sink.invalid and not sink.rows


@pytest.mark.parametrize("mode", ["single", "pending", "utf8"])
def test_overflow_is_still_invalid(mode):
    sink = CaseSink()
    if mode == "single":
        sink.write("x" * 65537)
    elif mode == "pending":
        sink.write("x" * 40000)
        sink.write("x" * 40000)
    else:
        record = probe_record()
        record["ignored_fixture_padding"] = "汉" * 25000
        text = PROBE_PREFIX + json.dumps(record, ensure_ascii=False) + "\n"
        assert len(text) <= 65536 and len(text.encode()) > 65536
        sink.write(text)
    assert sink.invalid and not sink.rows


def test_extra_frame_is_still_invalid_and_at_most_two_rows():
    sink = CaseSink()
    for case in ("A", "B", "A"):
        sink.write(frame(case))
    assert len(sink.rows) == 2 and sink.invalid


def test_incomplete_eof_remains_incomplete_by_original_main_rule():
    sink = CaseSink()
    sink.write(frame()[:-1])
    assert not sink.rows
    assert sink.invalid or sink.pending.startswith(PROBE_PREFIX)


@pytest.mark.parametrize("noise", ["F", "literal noise ", "echo "])
def test_prefix_embedded_in_same_write_is_not_a_new_publication_boundary(noise):
    sink = CaseSink()
    sink.write(noise + frame())
    assert not sink.rows


def test_wrong_platform_or_hook_count_never_becomes_a_case():
    for field, value in (
        ("platform", "darwin"),
        ("installed_hooks", 12),
        ("installed_hooks", True),
    ):
        record = probe_record()
        record[field] = value
        sink = CaseSink()
        sink.write("F")
        sink.write(PROBE_PREFIX + json.dumps(record) + "\n")
        assert sink.invalid and not sink.rows


def test_noise_and_frame_body_never_enter_finite_rows():
    sink = CaseSink()
    sink.write("FINITE_PRIVATE_CANARY")
    sink.write(frame())
    assert len(sink.rows) == 1
    assert "FINITE_PRIVATE_CANARY" not in json.dumps(sink.rows)
    assert "不得输出" not in json.dumps(sink.rows)


@pytest.mark.parametrize(
    "pending",
    [
        "F" + PROBE_PREFIX + "{",
        "F" + PROBE_PREFIX + '{"schema":',
        PROBE_PREFIX[:5],
        PROBE_PREFIX[:-1],
        "F" + PROBE_PREFIX[:5],
        "F" + PROBE_PREFIX[:-1],
    ],
)
def test_pending_protocol_or_partial_marker_is_sticky_invalid(pending):
    sink = CaseSink()
    sink.write(pending)
    sink.write(frame("A"))
    sink.write(frame("B"))
    assert sink.invalid
    assert len(sink.rows) <= 2


def test_v3_unverified_cases_do_not_upgrade_complete_branch_gate(tmp_path):
    from scripts.windows_git_native_branch_observation import observe
    from tests.governance.test_windows_git_native_branch_observation import witness

    rows = []
    for case in ("A", "B"):
        sink = CaseSink()
        sink.write(frame(case))
        row = sink.rows[0]
        row["raw_validation"] = "UNAVAILABLE"
        rows.append(row)
    (tmp_path / "cases.json").write_text(
        json.dumps({"pytest_exit": 1, "cases": rows, "invalid": False})
    )
    (tmp_path / "cdb-private.log").write_text(witness([(1, "FSTAT", -1), (2, "FSTAT", -1)]))
    result = observe.observation_result(
        tmp_path, {"debugger_exit": 1, "timed_out": False, "log_limit_stopped": False}
    )
    assert result["cases"] == [] and result["branch_gate_passed"] is False
    assert result["branch_witness"]["two_material_invocations_witnessed"] is True
    new = result["unverified_execution_observation"]
    assert new["assurance"] == "UNAUTHENTICATED_DIAGNOSTIC_ONLY"
    assert new["case_shape_state"] == "FINITE_AB" and len(new["cases"]) == 2
    assert new["marker_counts"] == {
        "arm_fullmatch_count": 2,
        "branch_fullmatch_count": 2,
        "bad_marker_prefix_count": 0,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "unavailable",
        "verified",
        "invalid",
        "extra_top",
        "extra_row",
        "reverse",
        "truncated",
        "timed_out",
        "debugger_fail",
    ],
)
def test_old_gate_differential_is_identical_after_removing_v3_observation(
    tmp_path, old_observe, kind
):
    rows = finite_rows()
    invalid, extra, state = False, {}, execution()
    if kind == "unavailable":
        for row in rows:
            row["raw_validation"] = "UNAVAILABLE"
    elif kind == "invalid":
        invalid = True
    elif kind == "extra_top":
        extra["body"] = "FINITE_BODY_CANARY"
    elif kind == "extra_row":
        rows[0]["body"] = "FINITE_BODY_CANARY"
    elif kind == "reverse":
        rows.reverse()
    elif kind == "truncated":
        rows[0]["diagnostic_truncated"] = True
    elif kind == "timed_out":
        state["timed_out"] = True
    elif kind == "debugger_fail":
        state["debugger_exit"] = 2
    write_report(tmp_path, rows, invalid, **extra)
    old = old_observe.observation_result(tmp_path, state)
    current = observe.observation_result(tmp_path, state)
    observation = current.pop("unverified_execution_observation")
    assert current == old
    if kind == "extra_top":
        assert old["branch_gate_passed"] is True
        assert observation["case_shape_state"] == "REJECTED"
    assert "FINITE_BODY_CANARY" not in json.dumps(observation)


@pytest.mark.parametrize(
    "field,value",
    [
        ("case", []),
        ("call", 1),
        ("worker_return", True),
        ("git_return", False),
        ("raw_validation", 1),
        ("proof", []),
        ("original_operation_returned", 1),
        ("diagnostic_incomplete", 0),
        ("diagnostic_truncated", None),
    ],
)
def test_new_case_observation_requires_exact_types(field, value):
    row = finite_rows()[0]
    row[field] = value
    assert projection.case_observation_valid(row) is False


@pytest.mark.parametrize("field", ["case", "call", "raw_validation", "proof"])
def test_new_text_fields_reject_subclasses_without_changing_old_membership(field):
    class Text(str):
        pass

    row = finite_rows()[0]
    row[field] = Text(row[field])
    assert projection.case_valid(row) is True
    assert projection.case_observation_valid(row) is False


@pytest.mark.parametrize(
    "kind", ["empty", "one", "third", "duplicate", "invalid_bool", "pytest_bool"]
)
def test_case_shape_and_source_invalid_are_independent(tmp_path, kind):
    rows = finite_rows()
    if kind == "empty":
        rows = []
    elif kind == "one":
        rows = rows[:1]
    elif kind == "third":
        rows.append(rows[0])
    elif kind == "duplicate":
        rows[1] = rows[0]
    write_report(tmp_path, rows, invalid=1 if kind == "invalid_bool" else True)
    if kind == "pytest_bool":
        report = json.loads((tmp_path / "cases.json").read_bytes())
        report["pytest_exit"] = True
        (tmp_path / "cases.json").write_text(json.dumps(report))
    result = observe.observation_result(tmp_path, execution())
    observation = result["unverified_execution_observation"]
    assert observation["case_shape_state"] == ("EMPTY" if kind == "empty" else "REJECTED")
    assert not result["branch_gate_passed"]
    assert observation["source_case_report_invalid"] is (None if kind == "invalid_bool" else True)


@pytest.mark.parametrize("kind", ["missing", "oversize", "empty"])
def test_log_unavailable_is_null_not_measured_zero(tmp_path, kind):
    write_report(tmp_path, finite_rows())
    log = tmp_path / "cdb-private.log"
    if kind == "missing":
        log.unlink()
    elif kind == "oversize":
        with log.open("wb") as stream:
            stream.truncate(observe.LOG_LIMIT + 1)
    else:
        log.write_text("")
    result = observe.observation_result(tmp_path, execution())
    observation = result["unverified_execution_observation"]
    assert observation["marker_state"] == ("MEASURED" if kind == "empty" else "NOT_AVAILABLE")
    assert observation["marker_counts"] == (
        {"arm_fullmatch_count": 0, "branch_fullmatch_count": 0, "bad_marker_prefix_count": 0}
        if kind == "empty"
        else None
    )
    assert not result["branch_gate_passed"]


def test_bad_decoding_keeps_original_failure_no_error_body(tmp_path, old_observe):
    write_report(tmp_path, finite_rows())
    (tmp_path / "cdb-private.log").write_bytes(b"\xff")
    for module in (old_observe, observe):
        with pytest.raises(UnicodeError):
            module.observation_result(tmp_path, execution())
    assert observe.unverified_observation()["marker_counts"] is None


@pytest.mark.parametrize(
    "kind,count", [("arm", 0), ("arm", 1), ("arm", 64), ("arm", 65), ("branch", 65), ("bad", 65)]
)
def test_marker_counts_are_syntax_only_saturated_lower_bounds(kind, count, old_projection):
    line = {
        "arm": "FHX_NATIVE_ARM pid=1",
        "branch": "FHX_NATIVE_BRANCH pid=1 tid=1 phase=FSTAT rva=70b74 fd=0 path=0 flags=1 ret=-1",
        "bad": "FHX_NATIVE_ARM MALFORMED_CANARY",
    }[kind]
    current, counts = projection._scan_branch_records("\n".join([line] * count))
    key = {
        "arm": "arm_fullmatch_count",
        "branch": "branch_fullmatch_count",
        "bad": "bad_marker_prefix_count",
    }[kind]
    assert counts[key] == min(64, count)
    assert all(type(value) is int and 0 <= value <= 64 for value in counts.values())
    assert current == projection.branch_records("\n".join([line] * count))
    assert current == old_projection.branch_records("\n".join([line] * count))
    assert not current["two_material_invocations_witnessed"]
    assert "pid" not in json.dumps(counts) and "CANARY" not in json.dumps(counts)


def test_fullmatch_wrong_context_is_not_bad_syntax_or_authority():
    log = witness([(1, "FSTAT", -1), (2, "FSTAT", -1)]).replace("fd=0", "fd=1")
    current, counts = projection._scan_branch_records(log)
    assert counts == {
        "arm_fullmatch_count": 2,
        "branch_fullmatch_count": 2,
        "bad_marker_prefix_count": 0,
    }
    assert not current["two_material_invocations_witnessed"]


def test_bad_marker_stays_invalid_before_later_complete_syntax(old_projection):
    log = "FHX_NATIVE_ARM MALFORMED\n" + witness([(1, "FSTAT", -1), (2, "FSTAT", -1)])
    current, counts = projection._scan_branch_records(log)
    assert counts == {
        "arm_fullmatch_count": 2,
        "branch_fullmatch_count": 2,
        "bad_marker_prefix_count": 1,
    }
    assert current == old_projection.branch_records(log)
    assert not current["two_material_invocations_witnessed"]


def test_arm_only_is_measured_not_a_branch_witness(tmp_path):
    write_report(tmp_path, finite_rows())
    (tmp_path / "cdb-private.log").write_text("FHX_NATIVE_ARM pid=1\nFHX_NATIVE_ARM pid=2\n")
    result = observe.observation_result(tmp_path, execution())
    assert result["unverified_execution_observation"]["marker_counts"] == {
        "arm_fullmatch_count": 2,
        "branch_fullmatch_count": 0,
        "bad_marker_prefix_count": 0,
    }
    assert result["branch_witness"]["invocations"] == []
    assert not result["branch_gate_passed"]


def test_case_and_log_read_only_once(tmp_path, monkeypatch):
    write_report(tmp_path, finite_rows())
    counts = {"text": 0, "bytes": 0}
    original_text, original_bytes = Path.read_text, Path.read_bytes

    def read_text(path, *args, **kwargs):
        counts["text"] += 1
        return original_text(path, *args, **kwargs)

    def read_bytes(path, *args, **kwargs):
        counts["bytes"] += 1
        return original_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    observe.observation_result(tmp_path, execution())
    assert counts == {"text": 1, "bytes": 1}


def test_nonwindows_v3_envelope_never_claims_execution_or_measurement(tmp_path):
    if observe.os.name == "nt":
        pytest.skip("仅非Windows原拒绝")
    report = tmp_path / "report"
    assert observe.run(ROOT, tmp_path / "output", report, False, "") == 2
    result = json.loads((report / "result.json").read_bytes())
    assert result["schema"] == "harnessix.git-native-branch-observation/v3"
    assert result["unverified_execution_observation"] == observe.unverified_observation()
    assert not result["execution_performed"] and not result["original_sdk_acceptance"]
    assert result["historical_root"] == "UNKNOWN"
