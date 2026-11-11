#!/bin/bash
# Round-2 development baseline on 247: oracle, the unchanged Harnessix bundle and mini-swe-agent on
# the new Multilingual dev set, 2 attempts each, then official grading and per-step records.
# The evaluation code and the bundle are the ones of the holdout30 formal run (code-447fe024,
# agent-efc184f0); only the task set, the dataset file and the spec are new. The spec lives outside
# the code directory so that the verified code copy stays untouched.
set -u
CODE_REV=447fe024
AGENT_REV=efc184f0
H=/data/harnessix/harbor
C=$H/code-$CODE_REV
A=$H/agent-$AGENT_REV
T=$H/tasks-multilingual-round2-dev-dbfb4109795a
R=$H/runs/multilingual-round2-dev-baseline-$CODE_REV
D=$H/round2/multilingual-round2-dev-full.jsonl
D_SHA256=a3f841b04349f783b362a91bf87c6369205a1058111f05f7cd17b44e857f88a6
S=$H/round2/dev-baseline.json
export PYTHONPATH=$C:$C/packages/evalkit/src LITELLM_LOCAL_MODEL_COST_MAP=True HARBOR_TELEMETRY=0
PY=/data/harnessix/harbor-venv/bin/python
P="$PY $C/integrations/harbor/pipeline.py"
log() { echo "$(date -Is) $*"; }

[ -e $R ] && { log "$R exists, refusing"; exit 1; }
for f in $A/harnessix-bundle.tar.gz $A/product.json $A/host-bundle/harnessix/bin/python $S; do
  [ -e $f ] || { log "missing $f, refusing"; exit 1; }
done
[ -e $T/prepare-receipt.json ] && [ "$(ls $T | grep -c __)" = 30 ] \
  || { log "task directory is not a finished preparation of 30 tasks, refusing"; exit 1; }
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
for arm in oracle harnessix R; do
  $P run --run-dir $R --arm $arm --code-root $C
  log "$arm finished"
done
$P finish --run-dir $R \
  --parser oracle=integrations.harbor.result_parsers:OracleParser \
  --parser harnessix=integrations.harnessix_eval.harbor_parser:HarnessixResultParser \
  --parser R=integrations.harbor.result_parsers:MiniSweAgentParser \
  --dataset $D --swebench /data/harnessix/swebench-venv/bin/swebench --workers 4 \
  --code-revision $CODE_REV > $R/finish.json
log "finish: $(cat $R/finish.json)"
mkdir -p $R/steps
$A/host-bundle/harnessix/bin/python -m integrations.harnessix_eval export-steps \
  --run-dir $R --trials $R/trials/harnessix.json --output-dir $R/steps --model qwen3.8-flash-next
$PY $C/integrations/harbor/mini_steps.py --run-dir $R --trials $R/trials/R.json --output-dir $R/steps
log "done"
