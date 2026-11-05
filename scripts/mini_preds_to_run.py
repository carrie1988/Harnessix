"""Convert a mini-swe-agent output directory to the local-run layout used by the scorer.

Writes predictions.jsonl (one row per expected task; a task mini-swe-agent did not submit gets an
empty patch, i.e. unresolved) and arm.json naming the reference arm.
usage: mini_preds_to_run.py OUTPUT_DIR TASK_LIST SPLIT ARM_NAME
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    out, task_list, split, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    document = json.loads(task_list.read_text(encoding="utf-8"))
    tasks = [
        task["instance_id"]
        for task in document["tasks"]
        if task.get("split", document.get("split")) == split
    ]
    preds_path = out / "preds.json"
    preds = json.loads(preds_path.read_text(encoding="utf-8")) if preds_path.exists() else {}
    with open(out / "predictions.jsonl", "w", encoding="utf-8") as handle:
        for instance in tasks:
            patch = (preds.get(instance) or {}).get("model_patch") or ""
            row = {"instance_id": instance, "model_name_or_path": name, "model_patch": patch}
            handle.write(json.dumps(row) + "\n")
    arm = {"name": name, "harnessix_revision": "mini-swe-agent-2.4.6", "dirty": False}
    (out / "arm.json").write_text(json.dumps(arm, indent=2) + "\n", encoding="utf-8")
    print(
        f"{name}: {sum(1 for i in tasks if (preds.get(i) or {}).get('model_patch'))}/{len(tasks)}"
        " with a patch"
    )


if __name__ == "__main__":
    main()
