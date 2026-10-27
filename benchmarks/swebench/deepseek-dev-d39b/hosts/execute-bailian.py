"""Frozen host for Bailian deepseek-v4-pro-0813 runs (d39 smoke, v63t, v63n): one attempt per case, no retry.

Usage (from the source root): execute-bailian.py <tag> <public plan directory> <scope> [stop-after-cases]
A run stopped by `swebench_billing_window_closed` (between cases) is resumed by launching the same command inside the
next billing window: the ledger allocation is reopened and finally settled with the cumulative cost of all cases."""

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, UTC
from pathlib import Path

from harnessix.agent.cancellation import CancelToken
from harnessix.evals.swebench.io import read_preregistration_plan
from harnessix.evals.swebench.run_config_io import read_swebench_agent_run_config
from harnessix.evals.swebench.execution import (
    run_swebench_agent_suite,
    _turn_cost,
    _unknown_request_reserve,
)
from harnessix.evals.task_pack_publication import (
    provider_publication_scope,
    open_task_pack_publication,
    TaskPackHistoryReadControl,
)
from harnessix.models.costs import build_cost_report, bind_price
from harnessix.models.pricing import amount_units, format_amount

TAG, PUBLIC_DIR, SCOPE = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
STOP_AFTER = int(sys.argv[4]) if len(sys.argv) > 4 else 0
BASE = Path("/Users/zhangjinhui/Library/Application Support/Harnessix/swebench")
PLAN = read_preregistration_plan(PUBLIC_DIR / "plan.json")
ROOT = BASE / "runs" / f"agent-{PLAN.plan_sha256[:12]}-{TAG}"
CONFIG = read_swebench_agent_run_config(ROOT / "run-config.json")
LEDGER = BASE / "budget-period-20261027-100-cny.json"
PERIOD = "d7a14312-1a8e-445a-8adb-2e171e336dcb"
KEY_FILE = BASE / "bailian-ws" / "api-key"
RESUMABLE = "swebench_billing_window_closed"
TERMINAL = {"completed", "failed", "cancelled"}


def now():
    return datetime.now(UTC).isoformat()


def write_json(path, data):
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _used(data):
    return sum(
        amount_units(a.get("known_cost_amount", "0")) + amount_units(a.get("reserved_cost_amount", "0"))
        if a["status"].startswith("settled_")
        else amount_units(a["maximum_amount"])
        for a in data["allocations"]
    )


def update_ledger(settlement=None):
    with open(BASE / ".budget-period.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(LEDGER.read_text())
        assert data["period_id"] == PERIOD
        matches = [
            a
            for a in data["allocations"]
            if a["plan_sha256"] == PLAN.plan_sha256 and a.get("execution_fingerprint") == CONFIG.fingerprint
        ]
        if settlement is None:
            if matches:
                # 只有停在Case之间的计价时段停止可续跑：重新按原停止线整额预留。
                (allocation,) = matches
                assert allocation["status"] == "settled_execution_stopped"
                assert allocation.get("stop_reason") == RESUMABLE
                others = [a for a in data["allocations"] if a is not allocation]
                assert _used({"allocations": others}) + amount_units(CONFIG.fee_stop_amount) <= amount_units(
                    data["limit_amount"]
                )
                allocation.update(status="reserved_execution", resumed_at=now())
            else:
                assert _used(data) + amount_units(CONFIG.fee_stop_amount) <= amount_units(data["limit_amount"])
                data["allocations"].append(
                    {
                        "plan_sha256": PLAN.plan_sha256,
                        "execution_fingerprint": CONFIG.fingerprint,
                        "run_directory": str(ROOT.relative_to(BASE)),
                        "maximum_amount": CONFIG.fee_stop_amount,
                        "status": "reserved_execution",
                        "reserved_at": now(),
                        "failure_policy": "freeze_transport_with_full_reserve",
                    }
                )
        else:
            assert len(matches) == 1 and matches[0]["status"] == "reserved_execution"
            total = amount_units(settlement["known_cost_upper_amount"]) + amount_units(
                settlement["reserved_cost_amount"]
            )
            assert total <= amount_units(matches[0]["maximum_amount"])
            matches[0].update(
                status="settled_execution_completed"
                if settlement["complete_predictions"]
                else "settled_execution_stopped",
                known_cost_amount=settlement["known_cost_upper_amount"],
                reserved_cost_amount=settlement["reserved_cost_amount"],
                stop_reason=settlement["stop_reason"],
                settled_at=now(),
            )
        write_json(LEDGER, data)


async def native_summary(key, result, stop_reason):
    known = reserved = 0
    cases = []
    failures = {}
    for index, case in enumerate(PLAN.cases):
        run = ROOT / "cases" / f"case-{index:03d}" / "run"
        if not (run / "session.sqlite").exists():
            continue
        token = CancelToken()
        with provider_publication_scope(CONFIG.provider_config.api_key_env, key) as scope:
            async with open_task_pack_publication(
                run, scope, history_read=TaskPackHistoryReadControl.begin(token)
            ) as owner:
                thread = await owner.authenticated_single_thread(TaskPackHistoryReadControl.begin(token))
        if thread is None:
            continue
        assert len(thread.turns) == 1
        turn = thread.turns[0]
        assert turn.status.value in {"completed", "failed", "cancelled", "interrupted"}
        units, complete = _turn_cost(turn, CONFIG)
        known += units
        report = build_cost_report(
            turn, tuple(bind_price(a, CONFIG.price, CONFIG.billing_context) for a in turn.accounted_attempts)
        )
        missing = (
            sum(e.result.status != "estimated" for e in report.entries)
            + len(report.summary.uncovered_steps)
            + sum(c.unaccounted_request_possible and c.attempt is None for c in turn.compactions)
        )
        if not complete:
            reserved += max(1, missing) * _unknown_request_reserve(CONFIG)
        state_path = ROOT / "cases" / f"case-{index:03d}" / "case-state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        code = turn.error.code if turn.error else None
        if code:
            failures[code] = failures.get(code, 0) + 1
        reasoning = sum((a.usage.reasoning_output_tokens or 0) for a in turn.model_attempts if a.usage)
        cases.append(
            {
                "instance_id": case.instance_id,
                "status": turn.status.value,
                "case_state_status": state.get("status"),
                "error_code": code,
                "case_error_code": state.get("error_code"),
                "input_tokens": turn.usage.input_tokens,
                "output_tokens": turn.usage.output_tokens,
                "reasoning_output_tokens": reasoning,
                "model_steps": turn.model_steps,
                "accounted_attempts": len(turn.accounted_attempts),
                "compaction_attempts": [
                    {
                        "status": record.status,
                        "error_code": record.failure.code if record.failure else None,
                        "input_tokens": record.attempt.usage.input_tokens if record.attempt else None,
                        "output_tokens": record.attempt.usage.output_tokens if record.attempt else None,
                        "usage_completeness": record.attempt.usage.completeness if record.attempt else None,
                    }
                    for record in turn.compactions
                ],
                "started_at": turn.created_at.isoformat() if getattr(turn, "created_at", None) else None,
                "known_cost_upper_amount": format_amount(units),
                "cost_complete": complete,
                "unaccounted_attempts": missing,
                "patch_sha256": state.get("patch_sha256"),
            }
        )
    predictions = ROOT / "predictions.jsonl"
    return {
        "schema": "harnessix.swebench-development-execution-summary/v1",
        "generated_at": now(),
        "plan_sha256": PLAN.plan_sha256,
        "source_revision": PLAN.harnessix_revision,
        "evaluation_scope": SCOPE,
        "attempted_cases": len(cases),
        "not_attempted_cases": 25 - len(cases),
        "nonempty_patch_cases": sum(
            c["patch_sha256"] not in {None, hashlib.sha256(b"").hexdigest()} for c in cases
        ),
        "known_cost_upper_amount": format_amount(known),
        "reserved_cost_amount": format_amount(reserved),
        "complete_predictions": predictions.exists(),
        "predictions_sha256": hashlib.sha256(predictions.read_bytes()).hexdigest() if predictions.exists() else None,
        "official_score": None,
        "stop_reason": stop_reason,
        "failure_counts": failures,
        "runtime_summary": result,
        "cases": cases,
    }


async def monitor(token):
    previous = None
    while True:
        free = shutil.disk_usage(BASE).free
        if free < 10 * 1024**3:
            token.cancel()
        path = ROOT / "suite-state.json"
        if path.exists():
            try:
                s = json.loads(path.read_text())
                finished = sum(c["status"] in TERMINAL for c in s["case_states"])
                current = (s["status"], len(s["completed_case_ids"]), finished)
                # 冒烟只执行前N个Case：第N个进入终态后立即取消，下一Case在物化阶段即停止。
                if STOP_AFTER and finished >= STOP_AFTER:
                    print(json.dumps({"stop_after_reached": finished}), flush=True)
                    token.cancel()
                # 系统性故障保护：尚无正常完成而已有3项失败结束时取消整轮，避免耗尽题集。
                failed = [c for c in s["case_states"] if c["status"] == "failed"]
                if not s["completed_case_ids"] and len(failed) >= 3:
                    print(json.dumps({"systemic_failure": [c.get("error_code") for c in failed]}), flush=True)
                    token.cancel()
                if current != previous:
                    print(json.dumps({"progress": current, "at": now(), "free_gib": round(free / 1024**3, 2)}), flush=True)
                    previous = current
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(2)


async def main():
    update_ledger()
    key = result = reason = watch = None
    token = CancelToken()
    try:
        # 百炼Key只存于0600私有文件；只读入内存，不打印、不写入其他位置。
        assert KEY_FILE.stat().st_mode & 0o077 == 0
        key = KEY_FILE.read_text().strip()
        assert key
        watch = asyncio.create_task(monitor(token))
        try:
            result = await run_swebench_agent_suite(
                PLAN, CONFIG, api_key=key, allow_repository_network=False, allow_provider_network=True, cancel=token
            )
        except Exception as error:
            reason = getattr(error, "code", type(error).__name__)
            print(json.dumps({"stopped": reason}), flush=True)
    finally:
        if watch:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        try:
            summary = await native_summary(key, result, reason)
            write_json(ROOT / "execution-summary.json", summary)
            update_ledger(summary)
            print(
                json.dumps(
                    {
                        k: summary[k]
                        for k in [
                            "attempted_cases",
                            "nonempty_patch_cases",
                            "known_cost_upper_amount",
                            "reserved_cost_amount",
                            "complete_predictions",
                            "stop_reason",
                        ]
                    }
                ),
                flush=True,
            )
        finally:
            key = None
    return 0 if result is not None else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
