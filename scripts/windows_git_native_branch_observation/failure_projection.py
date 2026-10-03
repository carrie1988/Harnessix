"""只接收原v5已发布的三个有限字段；与原九字段案例和完整门隔离。"""

from __future__ import annotations

import json

from scripts.windows_git_native_branch_observation.contract import unique_object
from scripts.windows_git_native_branch_observation.projection import (
    PROBE_PREFIX,
    case_observation_valid,
)

SCHEMA = "harnessix.git-native-failure-observation/v1"
ASSURANCE = "UNAUTHENTICATED_DIAGNOSTIC_ONLY"
SIGNAL_FIELDS = (
    "worker_failure_literal",
    "git_temp_create_prefix",
    "git_object_db_permission_prefix",
    "git_malformed_object_literal",
    "READ_ERROR",
    "SHORT_READ",
    "HASH_FD",
    "LOOSE_WRITE",
    "LOOSE_CLOSE",
)
TRACE_FIELDS = {
    "profile_status": frozenset({"OFF", "UNAVAILABLE", "MATCHED", "MISMATCH"}),
    "completeness": frozenset({"KNOWN", "UNKNOWN"}),
    "reason": frozenset(
        "NONE RAW_UNAVAILABLE LIMIT PROFILE_MISMATCH STREAM_BINDING_MISMATCH "
        "UNKNOWN_EVENT_OR_SCHEMA UNEXPECTED_EXECUTION_EVENT UNCLASSIFIED_FORMAT "
        "RETURN_UNAVAILABLE MISSING_EVENTS RETURN_MISMATCH MALFORMED_FRAME "
        "UNCLASSIFIED_STDERR".split()
    ),
    "return_consistency": frozenset({"UNAVAILABLE", "MISMATCH", "MATCHED_ZERO", "MATCHED_128"}),
    "source_profile_id": frozenset({None, "harnessix.git-material-trace2-profile/v1"}),
}
WITNESSES = ("ENTRY_START_MATCHED", "DISPATCH_HASH_OBJECT", "REPO_EVENT_SEEN")
FORMAT_IDS = tuple(
    sorted(
        "SETUP_EXPLICIT_NOT_REPOSITORY SETUP_GITDIR_ENV_BOUND HASH_OBJECT_ADD_AGGREGATE "
        "HASH_OBJECT_HASH_AGGREGATE OBJECT_FORMAT_MALFORMED GENERIC_DYNAMIC_FORMAT".split()
        + [
            "OBJECT_INDEX_SHORT_READ",
            "OBJECT_DATABASE_ADD_PERMISSION",
            "OBJECT_FINALIZE_PERMISSION",
        ]
    )
)
FAILURE_FIELDS = ("post_stderr_signals", "post_worker_failure_status", "post_git_trace2")
LEGACY_REPORT_FIELDS = frozenset({"pytest_exit", "cases", "invalid"})
SIBLING_KEY = "unverified_failure_observation"


def _ordered_subset(value: object, choices: tuple[str, ...]) -> bool:
    return (
        type(value) is list
        and len(value) <= len(choices)
        and all(type(item) is str and item in choices for item in value)
        and value == [item for item in choices if item in value]
    )


def _signals_valid(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == set(SIGNAL_FIELDS)
        and all(type(value[key]) is bool for key in SIGNAL_FIELDS)
    )


def _trace_valid(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == set(TRACE_FIELDS) | {"stage_witnesses", "error_format_ids"}
        and all(
            (type(value[key]) is str or (key == "source_profile_id" and value[key] is None))
            and value[key] in choices
            for key, choices in TRACE_FIELDS.items()
        )
        and _ordered_subset(value["stage_witnesses"], WITNESSES)
        and _ordered_subset(value["error_format_ids"], FORMAT_IDS)
    )


def _field_valid(key: str, value: object) -> bool:
    if key == "post_stderr_signals":
        return _signals_valid(value)
    if key == "post_git_trace2":
        return _trace_valid(value)
    return type(value) is str and value in {"not_observed", "invalid", "valid"}


def _copy_field(key: str, value: object) -> object:
    """只重建已通过精确合同的固定键；不携带任意原对象别名。"""
    if key == "post_stderr_signals":
        return {name: value[name] for name in SIGNAL_FIELDS}
    if key == "post_git_trace2":
        return {
            **{name: value[name] for name in TRACE_FIELDS},
            "stage_witnesses": list(value["stage_witnesses"]),
            "error_format_ids": list(value["error_format_ids"]),
        }
    return value


def project_failure_case(line: str) -> dict:
    """调用方先完成原project_case；再次解析同一内存帧，不读取原raw。"""
    record = json.loads(line[len(PROBE_PREFIX) :], object_pairs_hook=unique_object)
    operation = next(item for item in record["operations"] if item.get("kind") == "write")
    row = {"case": record["selector"], "field_states": {}}
    for key in FAILURE_FIELDS:
        state = "NOT_AVAILABLE" if key not in operation else "REJECTED"
        row[key] = None
        if key in operation and _field_valid(key, operation[key]):
            state = "FINITE"
            row[key] = _copy_field(key, operation[key])
        row["field_states"][key] = state
    return row


def _row_valid(row: object) -> bool:
    if (
        type(row) is not dict
        or set(row) != {"case", "field_states", *FAILURE_FIELDS}
        or type(row["case"]) is not str
        or row["case"] not in {"A", "B"}
        or type(row["field_states"]) is not dict
        or set(row["field_states"]) != set(FAILURE_FIELDS)
    ):
        return False
    return all(
        type(row["field_states"][key]) is str
        and (
            (row["field_states"][key] == "FINITE" and _field_valid(key, row[key]))
            or (row["field_states"][key] in {"NOT_AVAILABLE", "REJECTED"} and row[key] is None)
        )
        for key in FAILURE_FIELDS
    )


def failure_observation(rows: list | None = None, state: str = "NOT_AVAILABLE") -> dict:
    return {
        "schema": SCHEMA,
        "assurance": ASSURANCE,
        "case_shape_state": state,
        "cases": [] if rows is None else rows,
    }


def failure_report(rows: list) -> dict:
    if len(rows) == 2 and all(_row_valid(row) for row in rows):
        if [row["case"] for row in rows] == ["A", "B"]:
            return failure_observation(rows, "FINITE_AB")
    return failure_observation(state="EMPTY" if not rows else "REJECTED")


def isolate_failure_report(report: dict) -> tuple[dict, dict]:
    """仅剥离精确Sibling封装；坏Sibling不改变原案例、旧门或异常路径。"""
    if SIBLING_KEY not in report:
        return report, failure_observation()
    rejected = failure_observation(state="REJECTED")
    if set(report) != LEGACY_REPORT_FIELDS | {SIBLING_KEY}:
        return report, rejected
    legacy = {key: report[key] for key in LEGACY_REPORT_FIELDS}
    sibling = report[SIBLING_KEY]
    if (
        type(legacy["cases"]) is not list
        or type(legacy["invalid"]) is not bool
        or type(legacy["pytest_exit"]) is not int
        or not 0 <= legacy["pytest_exit"] <= 5
        or type(sibling) is not dict
        or set(sibling) != {"schema", "assurance", "case_shape_state", "cases"}
        or type(sibling["schema"]) is not str
        or sibling["schema"] != SCHEMA
        or type(sibling["assurance"]) is not str
        or sibling["assurance"] != ASSURANCE
        or type(sibling["case_shape_state"]) is not str
        or sibling["case_shape_state"] not in {"FINITE_AB", "EMPTY", "REJECTED"}
        or type(sibling["cases"]) is not list
    ):
        return legacy, rejected
    if sibling["case_shape_state"] != "FINITE_AB":
        if sibling["cases"]:
            return legacy, rejected
        if sibling["case_shape_state"] == "EMPTY" and legacy["cases"] != []:
            return legacy, rejected
        return legacy, failure_observation(state=sibling["case_shape_state"])
    rows, cases = sibling["cases"], legacy["cases"]
    if (
        len(cases) != 2
        or not all(case_observation_valid(row) for row in cases)
        or [row["case"] for row in cases] != ["A", "B"]
        or len(rows) != 2
        or not all(_row_valid(row) for row in rows)
        or [row["case"] for row in rows] != ["A", "B"]
    ):
        return legacy, rejected
    copied = [
        {
            "case": row["case"],
            "field_states": {key: row["field_states"][key] for key in FAILURE_FIELDS},
            **{
                key: _copy_field(key, row[key]) if row["field_states"][key] == "FINITE" else None
                for key in FAILURE_FIELDS
            },
        }
        for row in rows
    ]
    return legacy, failure_observation(copied, "FINITE_AB")
