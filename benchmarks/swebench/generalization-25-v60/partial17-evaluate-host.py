"""v60 partial official evaluation: only the 17 cases the stopped native run attempted.

Not the preregistered complete-25 metric. Patches come from HMAC-sealed workspace
retirement records and must match the suite case-state patch SHA; no case is rerun.
"""
import hashlib, json, os, subprocess, sys, time
from pathlib import Path

BASE = Path.home() / "Library/Application Support/Harnessix/swebench"
PUBLIC = Path("benchmarks/swebench/generalization-25-v60")
plan = json.loads((PUBLIC / "plan.json").read_text())
root = BASE / "runs" / f"agent-{plan['plan_sha256'][:12]}-v60"
suite = json.loads((root / "suite-state.json").read_text())
summary = json.loads((root / "execution-summary.json").read_text())
assert suite["plan_sha256"] == plan["plan_sha256"] and summary["complete_predictions"] is False
attempted = [s for s in suite["case_states"] if s.get("turn_id")]
assert len(attempted) == summary["attempted_cases"] == 17
rows = []
unsealed = []
for index, state in enumerate(attempted):
    assert state["instance_id"] == plan["cases"][index]["instance_id"]
    sealed_path = root / "cases" / f"case-{index:03d}" / "sealed-workspace.json"
    sealed = json.loads(sealed_path.read_text()) if sealed_path.exists() else None
    if sealed is None and state["patch_sha256"] != hashlib.sha256(b"").hexdigest():
        # 运行在该Case进行中被停止，补丁未经工作区封存，不能作为可信预测；记为排除而非空补丁。
        unsealed.append(state["instance_id"])
        continue
    if sealed is None:
        patch = ""
    else:
        patch = sealed["patch"]
        assert sealed["instance_id"] == state["instance_id"]
    assert hashlib.sha256(patch.encode()).hexdigest() == state["patch_sha256"], state["instance_id"]
    rows.append({"instance_id": state["instance_id"], "model_name_or_path": suite["model_name"], "model_patch": patch})
body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode()
digest = hashlib.sha256(body).hexdigest()
work = BASE / "runs" / f"official-{plan['plan_sha256'][:12]}-v60-partial17"
work.mkdir(mode=0o700)
pred = work / "predictions-partial17.jsonl"
fd = os.open(pred, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as f:
    f.write(body)
run_id = f"harnessix-{plan['plan_sha256'][:12]}-partial17-{digest[:12]}"
command = [str(BASE / "official-source/.venv/bin/swebench"), "eval", plan["dataset_name"],
           "--predictions", str(pred), "--run-id", run_id, "--split", plan["split"],
           "--workers", "1", "--timeout", "1800"]
for r in rows:
    command += ["--instance", r["instance_id"]]
# 预构建镜像；不传--task-repo以免先删除再强制重建。
results = work / "logs" / "evaluation" / run_id / "results.json"
# 关停看护（v57p）：官方评测器写出results.json后可能因超时实例线程卡在退出；10分钟后终止，只读导入。
process = subprocess.Popen(command, cwd=work, stdin=subprocess.DEVNULL)
seen = None
while process.poll() is None:
    time.sleep(30)
    if results.exists():
        seen = seen or time.monotonic()
        if time.monotonic() - seen > 600:
            process.terminate()
            process.wait(60)
print(json.dumps({"returncode": process.returncode, "predictions_sha256": digest, "run_id": run_id}), flush=True)
data = json.loads(results.read_text())
assert sorted(data["submitted_ids"]) == sorted(r["instance_id"] for r in rows)
keys = ["submitted_instances", "completed_instances", "resolved_instances", "unresolved_instances",
        "empty_patch_instances", "error_instances", "infra_failure_instances", "ambiguous_failure_instances"]
out = {"scope": "v60_first_17_of_25_evidence_stop_diagnostic_not_preregistered_metric", "predictions_sha256": digest,
       "results_sha256": hashlib.sha256(results.read_bytes()).hexdigest(), **{k: data[k] for k in keys},
       "resolved_ids": data["resolved_ids"], "error_ids": data["error_ids"], "excluded_unsealed_ids": unsealed}
(BASE / "v60-partial17-official.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps(out), flush=True)
