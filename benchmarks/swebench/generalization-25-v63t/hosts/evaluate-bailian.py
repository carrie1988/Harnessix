"""Official evaluation of one Bailian run (run from the source root): `formal` for a complete fresh cohort (the
preregistered metric, verified import), `attempted` for a stopped/smoke run (diagnostic, sealed patches only).

Usage: evaluate-bailian.py <tag> <public plan directory> <formal|attempted>
Shutdown watchdog (from v57p): once results.json has existed for 10 minutes with the evaluator still alive, the
evaluator is terminated and the written artifacts are imported read-only; nothing is re-run or re-scored."""
import hashlib, json, os, signal, subprocess, sys, threading, time
from pathlib import Path
from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.io import read_preregistration_plan
from harnessix.evals.swebench.predictions import read_predictions
from harnessix.evals.swebench.official import build_official_evaluation_command, evaluation_run_id, run_official_evaluation, import_official_result
from harnessix.evals.swebench.disk_space import require_evaluation_disk_space

TAG, PUBLIC, MODE = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
assert MODE in {"formal", "attempted"}
base = Path.home() / "Library/Application Support/Harnessix/swebench"
plan = read_preregistration_plan(PUBLIC / "plan.json")
root = base / "runs" / f"agent-{plan.plan_sha256[:12]}-{TAG}"
summary = json.loads((root / "execution-summary.json").read_text())
assert summary["plan_sha256"] == plan.plan_sha256
require_evaluation_disk_space(base)


def watch(results, pattern, done, fired):
    seen = None
    while not done.wait(30):
        if not results.exists():
            continue
        seen = seen or time.monotonic()
        if time.monotonic() - seen < 600:
            continue
        pids = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
        for pid in pids:
            fired.set()
            os.kill(int(pid), signal.SIGTERM)
        if pids:
            return


if MODE == "formal":
    assert summary["attempted_cases"] == 25 and summary["complete_predictions"], "cohort incomplete; no official score"
    predictions = read_predictions(root / "predictions.jsonl", plan)
    work = base / "runs" / f"official-{plan.plan_sha256[:12]}-{TAG}"
    work.mkdir(mode=0o700)
    run_id = evaluation_run_id(plan, predictions)
    results = work / "logs" / "evaluation" / run_id / "results.json"
    done, fired = threading.Event(), threading.Event()
    threading.Thread(target=watch, args=(results, f"swebench eval .*--run-id {run_id}", done, fired), daemon=True).start()
    command = build_official_evaluation_command(executable=base / "official-source/.venv/bin/swebench", swebench_source=base / "official-source", git_executable=Path("/usr/bin/git"), plan=plan, predictions_path=root / "predictions.jsonl", predictions=predictions, workers=1, timeout_seconds=1800, task_repo=base / "official-task-repo", rebuild_images=False)
    reason = "completed"
    try:
        run_official_evaluation(command=command, work_root=work, allow_network=True)
    except KernelError as error:
        if not (fired.is_set() and error.code == "swebench_evaluator_failed" and results.exists()):
            raise
        reason = "completed_evaluator_hung_at_shutdown_after_writing_results"
    finally:
        done.set()
    result, verified = import_official_result(work_root=work, plan=plan, predictions=predictions)
    data = json.loads(results.read_text())
    output = {"reason": reason, **verified.model_dump(mode="json"), "resolved_ids": sorted(data["resolved_ids"])}
else:
    suite = json.loads((root / "suite-state.json").read_text())
    attempted = [s for s in suite["case_states"] if s.get("turn_id")]
    rows, unsealed = [], []
    for index, state in enumerate(attempted):
        assert state["instance_id"] == plan.cases[index].instance_id
        sealed_path = root / "cases" / f"case-{index:03d}" / "sealed-workspace.json"
        sealed = json.loads(sealed_path.read_text()) if sealed_path.exists() else None
        if sealed is None and state["patch_sha256"] != hashlib.sha256(b"").hexdigest():
            # 运行在该Case进行中被停止，补丁未经工作区封存，不能作为可信预测；记为排除而非空补丁。
            unsealed.append(state["instance_id"])
            continue
        patch = "" if sealed is None else sealed["patch"]
        assert sealed is None or sealed["instance_id"] == state["instance_id"]
        assert hashlib.sha256(patch.encode()).hexdigest() == state["patch_sha256"], state["instance_id"]
        rows.append({"instance_id": state["instance_id"], "model_name_or_path": suite["model_name"], "model_patch": patch})
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode()
    digest = hashlib.sha256(body).hexdigest()
    work = base / "runs" / f"official-{plan.plan_sha256[:12]}-{TAG}-attempted"
    work.mkdir(mode=0o700)
    pred = work / "predictions-attempted.jsonl"
    fd = os.open(pred, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    run_id = f"harnessix-{plan.plan_sha256[:12]}-attempted-{digest[:12]}"
    command = [str(base / "official-source/.venv/bin/swebench"), "eval", plan.dataset_name, "--predictions", str(pred),
               "--run-id", run_id, "--split", plan.split, "--workers", "1", "--timeout", "1800"]
    for r in rows:
        command += ["--instance", r["instance_id"]]
    results = work / "logs" / "evaluation" / run_id / "results.json"
    done, fired = threading.Event(), threading.Event()
    threading.Thread(target=watch, args=(results, f"swebench eval .*--run-id {run_id}", done, fired), daemon=True).start()
    process = subprocess.run(command, cwd=work, stdin=subprocess.DEVNULL)
    done.set()
    data = json.loads(results.read_text())
    assert sorted(data["submitted_ids"]) == sorted(r["instance_id"] for r in rows)
    keys = ["submitted_instances", "completed_instances", "resolved_instances", "unresolved_instances",
            "empty_patch_instances", "error_instances"]
    output = {"scope": f"{TAG}_attempted_cases_diagnostic_not_preregistered_metric", "returncode": process.returncode,
              "predictions_sha256": digest, "results_sha256": hashlib.sha256(results.read_bytes()).hexdigest(),
              **{k: data[k] for k in keys}, "resolved_ids": data["resolved_ids"], "error_ids": data["error_ids"],
              "excluded_unsealed_ids": unsealed}
fd = os.open(base / f"{TAG}-official-{MODE}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(output, f, indent=2); f.write("\n")
print(json.dumps(output), flush=True)
