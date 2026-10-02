"""默认仅现场预检；显式固定revision授权后执行一次240秒外部调试观察。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import time
import zipfile
from pathlib import Path

from scripts.windows_git_native_branch_observation.contract import (
    CONTRACT_SHA256,
    read_contract,
    unique_object,
)
from scripts.windows_git_native_branch_observation.preflight import prepare, recheck
from scripts.windows_git_native_branch_observation.projection import branch_records, case_valid

LOG_LIMIT = 16 * 1024 * 1024


def execute_authorized(expected_revision: str) -> str:
    actual = os.environ.get("GITHUB_SHA", "")
    if (
        os.name != "nt"
        or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
        or not re.fullmatch(r"[0-9a-f]{40}", actual)
        or expected_revision != actual
    ):
        raise ValueError("explicit_fixed_revision_execution_required")
    return actual


def run_debugger(repository: Path, output: Path, state: dict) -> dict:
    # CDB未使用-pd；杀死调试器时由Windows保留默认调试对象终止策略。
    with (output / "execution-started.json").open("x", encoding="utf-8") as stream:
        json.dump({"watchdog_seconds": 240}, stream)
    timed_out = limited = False
    with (output / "console-private.log").open("xb") as console:
        deadline = time.monotonic() + 240
        process = subprocess.Popen(
            state["command"],
            cwd=repository,
            stdout=console,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
        )
        state["debugger_started"] = True
        try:
            while process.poll() is None:
                timed_out = time.monotonic() >= deadline
                limited = any(
                    path.is_file() and path.stat().st_size > LOG_LIMIT
                    for path in (output / "console-private.log", output / "cdb-private.log")
                )
                if timed_out or limited:
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.kill()
            debugger_exit = process.wait(timeout=5)
    return {"debugger_exit": debugger_exit, "timed_out": timed_out, "log_limit_stopped": limited}


def observation_result(output: Path, execution: dict) -> dict:
    branch_file = output / "cdb-private.log"
    case_file = output / "cases.json"
    witness = {"two_material_invocations_witnessed": False, "invocations": []}
    cases, pytest_exit, cases_valid = [], None, False
    if branch_file.is_file() and branch_file.stat().st_size <= LOG_LIMIT:
        witness = branch_records(branch_file.read_text(encoding="utf-8", errors="strict"))
    if case_file.is_file() and case_file.stat().st_size <= 8192:
        report = json.loads(case_file.read_bytes(), object_pairs_hook=unique_object)
        if type(report) is not dict:
            raise ValueError("case_report_invalid")
        cases, pytest_exit = report.get("cases", []), report.get("pytest_exit")
        cases_valid = (
            type(cases) is list
            and len(cases) == 2
            and all(case_valid(row) for row in cases)
            and [row.get("case") for row in cases] == ["A", "B"]
            and report.get("invalid") is False
            and type(pytest_exit) is int
            and pytest_exit in {0, 1}
            and all(
                row.get("raw_validation") == "OBSERVED_V5_VERIFIED_NOT_INDEPENDENT_MAC"
                and row.get("diagnostic_truncated") is False
                for row in cases
            )
        )
    complete = (
        witness["two_material_invocations_witnessed"]
        and cases_valid
        and type(execution["debugger_exit"]) is int
        and execution["debugger_exit"] in {0, 1}
        and not execution["timed_out"]
        and not execution["log_limit_stopped"]
    )
    return {
        **execution,
        "branch_gate_passed": complete,
        "branch_witness": witness,
        "pytest_exit": pytest_exit if type(pytest_exit) is int and 0 <= pytest_exit <= 5 else None,
        "cases": cases if cases_valid else [],
        "case_mapping": "SEQUENTIAL_A_B_NOT_OWNER_PID_ATTESTATION",
    }


def run(repository: Path, output: Path, report: Path, execute: bool, revision: str) -> int:
    report = report.resolve()
    repository = repository.resolve(strict=True)
    if report.is_relative_to(repository) or report.exists():
        raise ValueError("new_nonrepository_report_required")
    report.mkdir(parents=True)
    result = {
        "schema": "harnessix.git-native-branch-observation/v1",
        "status": "OFFLINE_METADATA_REFUSED",
        "execution_performed": False,
        "branch_gate_passed": False,
        "original_sdk_acceptance": False,
        "historical_run_result": "FAIL_RETAINED",
        "historical_root": "UNKNOWN",
        "metadata_sha256": CONTRACT_SHA256,
    }
    exit_code = 2
    state = {}
    try:
        contract = read_contract()
        result.update(budgets=contract["budgets"], status="PREFLIGHT_REFUSED")
        if execute:
            result["authorized_revision"] = execute_authorized(revision)
        state = prepare(repository, output, contract)
        result.update(
            source_input_count=len(state["source_checks"]),
            pairs_matched=True,
            cdb_sha256=state["cdb_sha256"],
            interpreter_sha256=state["python_sha256"],
            status="PREPARED_NOT_EXECUTED",
        )
        if not execute:
            exit_code = 0
        else:
            recheck(repository, state, contract)
            result.update(status="EXECUTION_INCOMPLETE")
            execution = run_debugger(repository, output, state)
            recheck(repository, state, contract)
            result.update(observation_result(output, execution))
            result["branch_gate_passed"] = (
                result["branch_gate_passed"] and state.get("debugger_started") is True
            )
            result["status"] = (
                "OBSERVED_NOT_PRODUCT_ACCEPTANCE"
                if result["branch_gate_passed"]
                else "EXECUTION_INCOMPLETE"
            )
            # 原两例失败仍使workflow失败；实际分支见证单独保留，不制造绿色业务结果。
            exit_code = result["pytest_exit"] if result["branch_gate_passed"] else 2
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        RecursionError,
        struct.error,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ):
        result["branch_gate_passed"] = False
        exit_code = 2
    result["execution_performed"] = state.get("debugger_started") is True
    body = (json.dumps(result, ensure_ascii=True, indent=2) + "\n").encode("ascii")
    (report / "result.json").write_bytes(body)
    (report / "result-sha256.json").write_text(
        json.dumps({"result_sha256": hashlib.sha256(body).hexdigest()}) + "\n", encoding="ascii"
    )
    print(
        json.dumps({"status": result["status"], "branch_gate_passed": result["branch_gate_passed"]})
    )
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expected-revision", default="")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    return run(args.repository, args.output, args.report, args.execute, args.expected_revision)


if __name__ == "__main__":
    raise SystemExit(main())
