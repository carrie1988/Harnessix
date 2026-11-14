#!/bin/bash
# Round-2 runs on 247 after the dev baseline: one spec, its arms one after the other, then official
# grading and per-step records. Used for the dev candidate reruns, the three test-set runs (mini,
# current version, candidate) and the candidate's regression runs on holdout30 and Verified dev25.
# Every check that fails refuses the run; an existing run directory is never overwritten.
#
# usage: run-arms.sh <run-name> <code-rev> <agent-rev|-> <spec.json> <tasks-dir> <task-count> \
#                    <dataset.jsonl> <dataset-sha256> <arm>...
#   code-rev   evaluation code directory /data/harnessix/harbor/code-<rev>
#   agent-rev  product bundle directory /data/harnessix/harbor/agent-<rev>; "-" when no arm is Harnessix
#   arm        arm ids of the spec, in run order; "R" is mini-swe-agent, every other id is Harnessix
set -u
[ $# -ge 9 ] || { echo "usage: run-arms.sh <run-name> <code-rev> <agent-rev|-> <spec> <tasks> <count> <dataset> <sha256> <arm>..."; exit 2; }
NAME=$1; CODE_REV=$2; AGENT_REV=$3; S=$4; T=$5; COUNT=$6; D=$7; D_SHA256=$8; shift 8
H=/data/harnessix/harbor
C=$H/code-$CODE_REV
A=$H/agent-$AGENT_REV
R=$H/runs/$NAME-$CODE_REV
export PYTHONPATH=$C:$C/packages/evalkit/src LITELLM_LOCAL_MODEL_COST_MAP=True HARBOR_TELEMETRY=0
PY=/data/harnessix/harbor-venv/bin/python
P="$PY $C/integrations/harbor/pipeline.py"
log() { echo "$(date -Is) $*"; }

[ -e $R ] && { log "$R exists, refusing"; exit 1; }
[ -e $S ] && [ -d $C/integrations/harbor ] || { log "missing spec or code directory, refusing"; exit 1; }
if [ "$AGENT_REV" != - ]; then
  grep -q "/agent-$AGENT_REV/" $S || { log "spec does not use agent-$AGENT_REV, refusing"; exit 1; }
  for f in $A/harnessix-bundle.tar.gz $A/product.json $A/host-bundle/harnessix/bin/python; do
    [ -e $f ] || { log "missing $f, refusing"; exit 1; }
  done
fi
[ -e $T/prepare-receipt.json ] && [ "$(ls $T | grep -c __)" = "$COUNT" ] \
  || { log "task directory is not a finished preparation of $COUNT tasks, refusing"; exit 1; }
echo "$D_SHA256  $D" | sha256sum -c --quiet || { log "dataset digest mismatch, refusing"; exit 1; }
for port in 18001 18002; do
  s=$(curl -s --max-time 5 "http://127.0.0.1:$port/metrics" \
    | awk '/^vllm:num_requests_(running|waiting)\{/{s += $2} END{print s + 0}')
  [ "$s" = 0 ] || { log "replica $port busy ($s), refusing"; exit 1; }
done
systemctl show harnessix-model-relay-balanced -p ExecStart --no-pager | grep -q -- '--docker-socket' \
  || { log "host relay does not run with --docker-socket, refusing"; exit 1; }
hot=$(/opt/hyhal/bin/hy-smi | awk '/^[0-9]+ +[0-9.]+C/{t=$2; sub("C","",t); if (t+0 >= 90) n++} END{print n+0}')
[ "$hot" = 0 ] || { log "DCU temperature >= 90 C, refusing"; exit 1; }

$P freeze --spec $S --tasks $T --run-dir $R --code-root $C --code-revision $CODE_REV \
  || { log "freeze failed"; exit 1; }
log "frozen"
parsers=()
for arm in "$@"; do
  $P run --run-dir $R --arm $arm --code-root $C
  log "$arm finished"
  if [ "$arm" = R ]; then
    parsers+=(--parser "$arm=integrations.harbor.result_parsers:MiniSweAgentParser")
  else
    parsers+=(--parser "$arm=integrations.harnessix_eval.harbor_parser:HarnessixResultParser")
  fi
done
$P finish --run-dir $R "${parsers[@]}" \
  --dataset $D --swebench /data/harnessix/swebench-venv/bin/swebench --workers 4 \
  --code-revision $CODE_REV > $R/finish.json
log "finish: $(cat $R/finish.json)"
mkdir -p $R/steps
for arm in "$@"; do
  if [ "$arm" = R ]; then
    $PY $C/integrations/harbor/mini_steps.py --run-dir $R --trials $R/trials/R.json --output-dir $R/steps
  else
    $A/host-bundle/harnessix/bin/python -m integrations.harnessix_eval export-steps \
      --run-dir $R --trials $R/trials/$arm.json --output-dir $R/steps --model qwen3.8-flash-next
  fi
done
log "done"
