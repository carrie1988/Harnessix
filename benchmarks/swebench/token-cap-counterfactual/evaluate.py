"""Official evaluation of token-cap counterfactual snapshots (diagnostic; no model calls)."""
import hashlib, json, subprocess, sys, time
from pathlib import Path
BASE = Path.home() / "Library/Application Support/Harnessix/swebench"; WORK = BASE / "cap-counterfactual"; cap = sys.argv[1]
pred = WORK / f"predictions-cap-{cap}.jsonl"; rows = [json.loads(l) for l in pred.read_text().splitlines() if l.strip()]
digest = hashlib.sha256(pred.read_bytes()).hexdigest(); run_id = f"harnessix-cap-{cap}-{digest[:12]}"
command = [str(BASE / "official-source/.venv/bin/swebench"), "eval", "SWE-bench/SWE-bench_Verified", "--predictions", str(pred),
           "--run-id", run_id, "--split", "test", "--workers", "1", "--timeout", "1800"]
for r in rows: command += ["--instance", r["instance_id"]]
results = WORK / "logs" / "evaluation" / run_id / "results.json"
process = subprocess.Popen(command, cwd=WORK, stdin=subprocess.DEVNULL); seen = None
while process.poll() is None:
    time.sleep(30)
    if results.exists():
        seen = seen or time.monotonic()
        if time.monotonic() - seen > 600: process.terminate(); process.wait(60)
data = json.loads(results.read_text()); assert sorted(data["submitted_ids"]) == sorted(r["instance_id"] for r in rows)
out = {"cap": int(cap), "predictions_sha256": digest, "run_id": run_id, "results_sha256": hashlib.sha256(results.read_bytes()).hexdigest(),
       **{k: data[k] for k in ("submitted_instances", "completed_instances", "resolved_instances", "error_instances")}, "resolved_ids": data["resolved_ids"], "error_ids": data["error_ids"]}
(WORK / f"official-cap-{cap}.json").write_text(json.dumps(out, indent=2) + "\n"); print(json.dumps(out), flush=True)
