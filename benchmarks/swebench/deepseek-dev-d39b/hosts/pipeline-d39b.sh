#!/bin/bash
# d39b (rerun of d39 after the Bailian tool_choice fix): mechanics smoke of run-config v39 (every-step thinking) on Bailian deepseek-v4-pro-0813 over the first 3
# development cases. Waits for images, freezes, preregisters, commits, executes, then evaluates the attempted cases.
set -u
B="/Users/zhangjinhui/Library/Application Support/Harnessix/swebench"
R=/Users/zhangjinhui/.codex/worktrees/r3-workflow-efficiency/Harnessix
P=benchmarks/swebench/deepseek-dev-d39b
log(){ echo "$(date -u +%H:%M:%S) $*"; }
missing(){ (cd "$R" && PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" python3 "$B/missing-images.py" "$P/plan.json"); }
for i in $(seq 1 600); do
  M=$(missing); [ -z "$M" ] && break
  pgrep -f "swebench images build" >/dev/null || { [ "$i" -gt 5 ] && { log "IMAGES_MISSING_NO_BUILDER $M"; exit 2; }; }
  sleep 30
done
[ -n "$M" ] && { log "IMAGES_TIMEOUT $M"; exit 2; }
log "ALL_IMAGES_OK"
cd "$R" || exit 3
[ -z "$(git status --short -- src tests)" ] || { log "DIRTY_TREE"; exit 3; }
OUT=$(PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B "$B/prepare-bailian.py" d39b "$P" v39 35 2>&1 | tail -1); log "PREPARE $OUT"
echo "$OUT" | grep -q '"spec": "harnessix.swebench-agent-run-config/v39"' || { log "PREPARE_FAILED"; exit 4; }
python3 "$B/preregister-bailian.py" d39b "$P" development "$(cat "$B/generalization-v63/d39-fields.json")" || { log "PREREGISTER_FAILED"; exit 5; }
git add "$P" && git commit -q -m "evals: freeze d39b smoke (after omit_tool_choice fix) of run-config v39 on Bailian deepseek-v4-pro-0813

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" || { log "COMMIT_FAILED"; exit 6; }
log "PREREGISTERED $(git log --oneline -1)"
PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B "$B/execute-bailian.py" d39b "$P" development_smoke_first_3_cases 3 > "$B/execute-d39b.log" 2>&1
log "EXECUTE_DONE $(tail -1 "$B/execute-d39b.log" | cut -c1-300)"
PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B "$B/evaluate-bailian.py" d39b "$P" attempted > "$B/evaluate-d39b.log" 2>&1
log "EVALUATE_DONE exit=$? $(tail -1 "$B/evaluate-d39b.log" | cut -c1-400)"
