#!/usr/bin/env bash
# Continue the week-1 holdout with the phase-2 robustness code after the frozen run was stopped.
#   A2x4 Verified (tasks the frozen run did not finish) -> R Verified -> A2x4 Multilingual
#   -> R Multilingual. Each arm is scored with the official evaluator as soon as it finishes.
# Tasks the frozen run finished stay in their original run directories; this script never
# touches them. Recorded as a deviation in <runs root>/DEVIATIONS.md.
# usage: run_phase2_continuation.sh <code worktree> <runs root> <remaining Verified task list>
# A later code switch reuses this script with its own Verified run name and revision tag:
#   VERIFIED_RUN=verified-A2x4-p3 REVISION_TAG=phase3 run_phase2_continuation.sh ...
set -euo pipefail

CODE=$1
RUNS=$2
REMAINING=$3
PY=/data/harnessix/Harnessix/.venv/bin/python
HX=/data/harnessix/Harnessix/.venv/bin/harnessix
SWEBENCH=/data/harnessix/swebench-venv/bin/swebench
DATA=/data/harnessix/work/data
LISTS=$CODE/benchmarks/swebench/week1-local
export PYTHONPATH=$CODE/src

cd "$CODE"
test -z "$(git status --porcelain --untracked-files=no)" || { echo "code tree is dirty" >&2; exit 2; }
VERIFIED_RUN=${VERIFIED_RUN:-verified-A2x4-p2}
git rev-parse HEAD > "$RUNS/REVISION.${REVISION_TAG:-phase2}"

score() {  # score <run dir> <full rows>
  (cd "$CODE" && $PY scripts/swebench_local_report.py evaluate "$1" --dataset "$2" \
      --swebench "$SWEBENCH" --workers 6 > "$1.eval.log" 2>&1) &
}

harnessix_arm() {  # harnessix_arm <arm> <task list> <split> <public rows> <full rows> <name>
  local out=$RUNS/$6
  $PY scripts/swebench_local_run.py "$2" --rows "$4" --split "$3" --arm "$1" --run-root "$out" \
      --ports 18002,18001 --per-replica 2 --harnessix "$HX" > "$out.log" 2>&1
  score "$out" "$5"
}

reference_arm() {  # reference_arm <dataset dir> <task list> <name> <full rows>
  local out=$RUNS/$3
  (cd "$RUNS" && MSWEA_SILENT_STARTUP=1 MSWEA_COST_TRACKING=ignore_errors \
      LITELLM_LOCAL_MODEL_COST_MAP=True HF_HUB_OFFLINE=1 \
      /data/harnessix/mini-venv/bin/mini-extra swebench --subset "$1" --split test \
      --filter "$(ids "$2" holdout)" -o "$out" -w 4 -c swebench.yaml \
      -c /data/harnessix/work/mini-r.yaml > "$out.log" 2>&1) || true
  $PY scripts/mini_preds_to_run.py "$out" "$2" holdout R
  score "$out" "$4"
}

ids() {  # ids <task list> <split> -> anchored regex of instance IDs
  $PY - "$1" "$2" <<'EOF'
import json, re, sys
document = json.load(open(sys.argv[1]))
tasks = [t["instance_id"] for t in document["tasks"]
         if t.get("split", document.get("split")) == sys.argv[2]]
print("^(" + "|".join(re.escape(t) for t in tasks) + ")$")
EOF
}

echo "$(date -Is) A2x4 Verified (remaining)" && harnessix_arm A2x4 "$REMAINING" holdout \
    "$DATA/verified-public.jsonl" "$DATA/verified-full.jsonl" "$VERIFIED_RUN"
echo "$(date -Is) R Verified" && reference_arm "$DATA/verified-dir" \
    "$LISTS/verified-holdout49.json" verified-R "$DATA/verified-full.jsonl"
echo "$(date -Is) A2x4 Multilingual" && harnessix_arm A2x4 "$LISTS/multilingual40.json" holdout \
    "$DATA/multilingual-public.jsonl" "$DATA/multilingual-full.jsonl" multilingual-A2x4
echo "$(date -Is) R Multilingual" && reference_arm "$DATA/multilingual-dir" \
    "$LISTS/multilingual40.json" multilingual-R "$DATA/multilingual-full.jsonl"
wait
echo "$(date -Is) done"
