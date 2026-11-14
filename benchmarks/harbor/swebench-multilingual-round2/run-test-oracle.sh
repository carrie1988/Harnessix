#!/bin/bash
# Round-2 test set environment check on 247: gold patches only (oracle arm), no agent, no model.
# usage: run-test-oracle.sh <task directory> <run suffix>
set -u
H=/data/harnessix/harbor
C=$H/code-447fe024
T=$1
R=$H/runs/multilingual-round2-test-oracle-$2
S=$H/round2/test-oracle.json
export PYTHONPATH=$C:$C/packages/evalkit/src HARBOR_TELEMETRY=0
P="/data/harnessix/harbor-venv/bin/python $C/integrations/harbor/pipeline.py"
log() { echo "$(date -Is) $*"; }
[ -e $R ] && { log "$R exists, refusing"; exit 1; }
[ -e $T/prepare-receipt.json ] || { log "$T is not a finished preparation, refusing"; exit 1; }
$P freeze --spec $S --tasks $T --run-dir $R --code-root $C --code-revision 447fe024 \
  || { log "freeze failed"; exit 1; }
$P run --run-dir $R --arm oracle --code-root $C
log "oracle finished"
for t in $R/jobs/oracle/*__*; do
  echo "$(basename $t | sed 's/__[^_]*$//') $(cat $t/verifier/reward.txt 2>/dev/null || echo none)"
done | sort > $R/oracle-rewards.txt
log "gold patch failed: $(grep -v ' 1' $R/oracle-rewards.txt | tr '\n' ';')"
