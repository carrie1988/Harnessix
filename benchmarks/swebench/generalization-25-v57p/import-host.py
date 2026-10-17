"""Import the official v57p results.json written before the evaluator hung at interpreter shutdown.

The evaluator printed its final report and wrote results.json, then blocked in
wait_for_thread_shutdown on the thread of the timed-out sphinx-8269 instance; it was
terminated after ~6 h idle. This only re-reads and verifies the official artifacts."""
import json, os
from pathlib import Path
from harnessix.evals.swebench.io import read_preregistration_plan
from harnessix.evals.swebench.predictions import read_predictions
from harnessix.evals.swebench.official import import_official_result
base = Path.home() / "Library/Application Support/Harnessix/swebench"
plan = read_preregistration_plan(Path("benchmarks/swebench/generalization-25-v57p/plan.json"))
root = base / "runs" / f"agent-{plan.plan_sha256[:12]}-v57p"
predictions = read_predictions(root / "predictions.jsonl", plan)
work = base / "runs" / f"official-{plan.plan_sha256[:12]}-v57p"
result, verified = import_official_result(work_root=work, plan=plan, predictions=predictions)
output = {"reason": "completed_evaluator_hung_at_shutdown_after_writing_results", **verified.model_dump(mode="json")}
fd = os.open(base / "v57p-official-verified.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(output, f, indent=2); f.write("\n")
print(json.dumps({k: v for k, v in output.items() if not isinstance(v, (list, dict))}))
