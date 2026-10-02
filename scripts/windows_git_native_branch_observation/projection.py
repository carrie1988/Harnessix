"""仅投影实际返回寄存器和原v5有限记录；不保留正文、路径或动态摘要。"""

from __future__ import annotations

import json
import re

from scripts.windows_git_native_branch_observation.contract import unique_object

ARM = re.compile(r"FHX_NATIVE_ARM pid=([1-9][0-9]{0,9})")
BRANCH = re.compile(
    r"FHX_NATIVE_BRANCH pid=([1-9][0-9]{0,9}) tid=([1-9][0-9]{0,9}) "
    r"phase=(FSTAT|INDEX) rva=([0-9a-f]{1,8}) fd=(-?[0-9]{1,10}) "
    r"path=([0-9a-f]{1,16}) flags=([0-9a-f]{1,8}) ret=(-?[0-9]{1,11})"
)
PROBE_PREFIX = "HX_MINIMUM_COMMIT_PROBE "
CASE_FIELDS = frozenset(
    "case call worker_return git_return raw_validation proof original_operation_returned "
    "diagnostic_incomplete diagnostic_truncated".split()
)


def case_valid(row: object) -> bool:
    return (
        type(row) is dict
        and set(row) == CASE_FIELDS
        and row["case"] in {"A", "B"}
        and row["call"] in {"passed", "failed"}
        and all(
            row[key] is None or integer(row[key]) is not None
            for key in ("worker_return", "git_return")
        )
        and row["raw_validation"] in {"OBSERVED_V5_VERIFIED_NOT_INDEPENDENT_MAC", "UNAVAILABLE"}
        and row["proof"] in {"ABSENT", "UNAVAILABLE", "OBSERVED_V5_VALID_NOT_PRODUCT_ACCEPTANCE"}
        and all(
            type(row[key]) is bool
            for key in (
                "original_operation_returned",
                "diagnostic_incomplete",
                "diagnostic_truncated",
            )
        )
    )


def branch_records(log: str) -> dict:
    armed, groups = set(), {}
    valid = True
    for line in log.splitlines():
        if match := ARM.fullmatch(line):
            pid = int(match[1])
            valid = valid and pid not in armed and pid < 2**32 and len(armed) < 64
            if len(armed) >= 64:
                continue
            armed.add(pid)
        elif match := BRANCH.fullmatch(line):
            pid, tid, phase, rva, fd, path, flags, ret = match.groups()
            pid, tid, value = int(pid), int(tid), int(ret)
            valid = valid and (
                pid in armed
                and tid < 2**32
                and -(2**31) <= value < 2**31
                and int(fd) == 0
                and int(path, 16) == 0
                and int(flags, 16) & 1 == 1
                and rva == {"FSTAT": "70b74", "INDEX": "70bc1"}[phase]
            )
            if len(groups) >= 2 and (pid, tid) not in groups:
                valid = False
                continue
            events = groups.setdefault((pid, tid), [])
            if len(events) >= 2:
                valid = False
            else:
                events.append((phase, value))
        elif line.startswith(("FHX_NATIVE_BRANCH", "FHX_NATIVE_ARM")):
            valid = False
    rows = []
    for events in groups.values():
        if events == [("FSTAT", -1)]:
            branch, index = "FSTAT_FAILED", "NOT_ENTERED"
        elif len(events) == 2 and events[0] == ("FSTAT", 0) and events[1][0] == "INDEX":
            branch = "INDEX_FAILED" if events[1][1] != 0 else "BOTH_SUCCEEDED"
            index = events[1][1]
        else:
            branch, index = "UNKNOWN", "UNAVAILABLE"
        valid = valid and branch != "UNKNOWN"
        fstat = events[0][1] if events[0][0] == "FSTAT" else None
        rows.append({"branch": branch, "fstat_return": fstat, "index_return": index})
    valid = valid and len(rows) == 2 and len({pid for pid, _ in groups}) == 2
    return {"two_material_invocations_witnessed": valid, "invocations": rows}


def integer(value: object) -> int | None:
    return value if type(value) is int and -(2**31) <= value < 2**32 else None


def raw_status(operation: dict) -> str:
    for raw_key, receipt_key in (("raw", "receipt"), ("post_raw", "post_receipt")):
        raw, receipt = operation.get(raw_key, {}), operation.get(receipt_key, {})
        if type(raw) is not dict or type(receipt) is not dict:
            continue
        if receipt.get("provenance") != "terminal_receipt_authenticated":
            continue
        if all(
            type(raw.get(name)) is dict
            and raw[name].get("eof") is True
            and raw[name].get("full_raw_verified") is True
            and type(receipt.get("raw_" + name)) is dict
            and receipt["raw_" + name].get("eof") is True
            and type(raw[name].get("bytes")) is int
            and raw[name]["bytes"] >= 0
            and raw[name]["bytes"] == receipt["raw_" + name].get("observed_bytes")
            and type(raw[name].get("sha256")) is str
            and re.fullmatch(r"[0-9a-f]{64}", raw[name]["sha256"]) is not None
            and raw[name]["sha256"] == receipt["raw_" + name].get("sha256")
            for name in ("stdout", "stderr")
        ):
            return "OBSERVED_V5_VERIFIED_NOT_INDEPENDENT_MAC"
    return "UNAVAILABLE"


def project_case(line: str) -> dict:
    if len(line.encode("utf-8")) > 65536 or not line.startswith(PROBE_PREFIX):
        raise ValueError("probe_frame_invalid")
    record = json.loads(line[len(PROBE_PREFIX) :], object_pairs_hook=unique_object)
    if (
        type(record) is not dict
        or record.get("schema") != "harnessix.minimum-commit-probe/v5"
        or record.get("selector") not in {"A", "B"}
        or record.get("platform") != "win32"
        or type(record.get("installed_hooks")) is not int
        or record["installed_hooks"] != 13
        or type(record.get("operations")) is not list
        or any(type(item) is not dict for item in record["operations"])
    ):
        raise ValueError("probe_frame_invalid")
    writes = [item for item in record["operations"] if item.get("kind") == "write"]
    if len(writes) != 1:
        raise ValueError("probe_frame_invalid")
    operation = writes[0]
    proof = operation.get("proof", operation.get("post_proof", {}))
    failure = operation.get("post_worker_failure", {})
    lease = operation.get("lease", {})
    outcomes = record.get("outcomes", {})
    if any(type(item) is not dict for item in (proof, failure, lease, outcomes)):
        raise ValueError("probe_frame_invalid")
    call = outcomes.get("call")
    if outcomes.get("setup") != "passed" or outcomes.get("teardown") != "passed":
        raise ValueError("probe_frame_invalid")
    if call not in {"passed", "failed"}:
        raise ValueError("probe_frame_invalid")
    proof_state = "UNAVAILABLE"
    if proof.get("status") == "absent":
        proof_state = "ABSENT"
    elif (
        all(
            proof.get(key) is True
            for key in (
                "contract_valid",
                "source_eof",
                "git_stdout_eof",
                "producer_pid_matches_lease",
            )
        )
        and integer(proof.get("git_returncode")) == 0
    ):
        proof_state = "OBSERVED_V5_VALID_NOT_PRODUCT_ACCEPTANCE"
    return {
        "case": record["selector"],
        "call": call,
        "worker_return": integer(lease.get("returncode")),
        "git_return": integer(proof.get("git_returncode", failure.get("git_returncode"))),
        "raw_validation": raw_status(operation),
        "proof": proof_state,
        "original_operation_returned": operation.get("original_operation_returned") is True,
        "diagnostic_incomplete": record.get("diagnostic_incomplete") is not False,
        "diagnostic_truncated": record.get("diagnostic_truncated") is not False,
    }
