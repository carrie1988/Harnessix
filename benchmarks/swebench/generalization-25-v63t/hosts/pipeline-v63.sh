#!/bin/bash
# v63: paired fresh measurement on Bailian deepseek-v4-pro-0813 - v63t (run-config v40, tool-step thinking) then v63n (v37).
# Waits for images, freezes both configs, preregisters both, commits, then executes each arm (resuming in the next
# Beijing idle window when a window closes between cases) and evaluates each complete arm officially.
set -u
B="/Users/zhangjinhui/Library/Application Support/Harnessix/swebench"
R=/Users/zhangjinhui/.codex/worktrees/r3-workflow-efficiency/Harnessix
T=benchmarks/swebench/generalization-25-v63t
N=benchmarks/swebench/generalization-25-v63n
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
log(){ echo "$(date -u +%H:%M:%S) $*"; }
py(){ (cd "$R" && PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B "$@"); }
missing(){ (cd "$R" && python3 "$B/missing-images.py" "$T/plan.json"); }
for i in $(seq 1 900); do
  M=$(missing); [ -z "$M" ] && break
  pgrep -f "swebench images build" >/dev/null || { [ "$i" -gt 5 ] && { log "IMAGES_MISSING_NO_BUILDER $M"; exit 2; }; }
  sleep 30
done
[ -n "$M" ] && { log "IMAGES_TIMEOUT $M"; exit 2; }
log "ALL_IMAGES_OK"
cd "$R" || exit 3
[ -z "$(git status --short -- src tests)" ] || { log "DIRTY_TREE"; exit 3; }
REMAINING=$(python3 -c "
import json;d=json.load(open('$B/budget-period-20261027-100-cny.json'))
u=sum(float(a.get('known_cost_amount',0))+float(a.get('reserved_cost_amount',0)) if a['status'].startswith('settled_') else float(a['maximum_amount']) for a in d['allocations'])
print(int(float(d['limit_amount'])-u-2))")
log "V63T_FEE_STOP $REMAINING"
OUT=$(py "$B/prepare-bailian.py" v63t "$T" v40 "$REMAINING" 2>&1 | tail -1); log "PREPARE_T $OUT"
echo "$OUT" | grep -q '"spec": "harnessix.swebench-agent-run-config/v40"' || { log "PREPARE_T_FAILED"; exit 4; }
OUT=$(py "$B/prepare-bailian.py" v63n "$N" v37 34 2>&1 | tail -1); log "PREPARE_N $OUT"
echo "$OUT" | grep -q '"spec": "harnessix.swebench-agent-run-config/v37"' || { log "PREPARE_N_FAILED"; exit 4; }
python3 "$B/preregister-bailian.py" v63t "$T" generalization "$(cat "$B/generalization-v63/v63t-fields.json")" || exit 5
python3 "$B/preregister-bailian.py" v63n "$N" generalization "$(cat "$B/generalization-v63/v63n-fields.json")" || exit 5
git add "$T" "$N" && git commit -q -m "evals: freeze paired fresh run v63 (v40 thinking vs v37) on Bailian deepseek-v4-pro-0813

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" || { log "COMMIT_FAILED"; exit 6; }
log "PREREGISTERED $(git log --oneline -1)"
run_arm(){ # tag dir
  while true; do
    py "$B/execute-bailian.py" "$1" "$2" fresh_generalization_set >> "$B/execute-$1.log" 2>&1
    STOP=$(tail -1 "$B/execute-$1.log" | python3 -c "import json,sys;print(json.loads(sys.stdin.read()).get('stop_reason'))" 2>/dev/null)
    log "EXECUTE_$1 stop=$STOP $(tail -1 "$B/execute-$1.log" | cut -c1-200)"
    [ "$STOP" = "swebench_billing_window_closed" ] || break
    WAIT=$(python3 -c "
from datetime import datetime,timedelta;from zoneinfo import ZoneInfo
n=datetime.now(ZoneInfo('Asia/Shanghai'));s=n.replace(hour=22,minute=1,second=0,microsecond=0)
s=s if s>n else s+timedelta(days=1);print(int((s-n).total_seconds()))")
    log "WINDOW_CLOSED sleeping ${WAIT}s"; sleep "$WAIT"
  done
  if grep -q '"complete_predictions": true' "$B/execute-$1.log"; then
    py "$B/evaluate-bailian.py" "$1" "$2" formal > "$B/evaluate-$1.log" 2>&1; log "EVALUATE_$1 exit=$? $(tail -1 "$B/evaluate-$1.log" | cut -c1-300)"
  else
    log "INCOMPLETE_$1 (no official score)"
  fi
}
run_arm v63t "$T"
run_arm v63n "$N"
log "PIPELINE_DONE"
