#!/bin/bash
# Resume the frozen v63n arm (run-config v37) in the next Beijing idle window after it stopped between cases on
# swebench_billing_window_closed, then evaluate it officially and delete the 25 v63 evaluation images once the
# official result is written. No configuration is changed; the execute host reopens the same ledger allocation.
set -u
B="/Users/zhangjinhui/Library/Application Support/Harnessix/swebench"
R=/Users/zhangjinhui/.codex/worktrees/r3-workflow-efficiency/Harnessix
N=benchmarks/swebench/generalization-25-v63n
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
log(){ echo "$(date -u +%H:%M:%S) $*"; }
py(){ (cd "$R" && PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B "$@"); }
until_window(){ python3 -c "
from datetime import datetime,timedelta;from zoneinfo import ZoneInfo
n=datetime.now(ZoneInfo('Asia/Shanghai'));s=n.replace(hour=22,minute=1,second=0,microsecond=0)
inside = n.hour >= 22 or n.hour < 7
print(0 if inside else int(((s if s>n else s+timedelta(days=1))-n).total_seconds()))"; }
for night in 1 2; do
  WAIT=$(until_window); log "WAIT ${WAIT}s"; sleep "$WAIT"
  [ -z "$(cd "$R" && git status --short -- src tests)" ] || { log "DIRTY_TREE"; exit 3; }
  py "$B/execute-bailian.py" v63n "$N" fresh_generalization_set >> "$B/execute-v63n.log" 2>&1
  STOP=$(tail -1 "$B/execute-v63n.log" | python3 -c "import json,sys;print(json.loads(sys.stdin.read()).get('stop_reason'))" 2>/dev/null)
  log "EXECUTE_v63n stop=$STOP $(tail -1 "$B/execute-v63n.log" | cut -c1-200)"
  [ "$STOP" = "swebench_billing_window_closed" ] || break
done
if tail -1 "$B/execute-v63n.log" | grep -q '"complete_predictions": true'; then
  py "$B/evaluate-bailian.py" v63n "$N" formal > "$B/evaluate-v63n.log" 2>&1; CODE=$?
  log "EVALUATE_v63n exit=$CODE $(tail -1 "$B/evaluate-v63n.log" | cut -c1-300)"
  if [ "$CODE" = 0 ] && [ -f "$B/v63n-official-formal.json" ]; then
    for id in $(python3 -c "import json;print(' '.join(c['instance_id'] for c in json.load(open('$R/$N/plan.json'))['cases']))"); do
      img="swebench/sweb.eval.x86_64.$(echo "$id" | sed 's/__/_1776_/' | tr 'A-Z' 'a-z'):latest"
      docker ps -a --filter "ancestor=$img" --format '{{.Names}}' | grep -q . && { log "IN_USE $img"; continue; }
      docker rmi "$img" >/dev/null 2>&1 && log "REMOVED $id"
    done
  fi
else
  log "INCOMPLETE_v63n (no official score)"
fi
log "RESUME_DONE"
