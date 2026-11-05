#!/usr/bin/env bash
# Container entrypoint: serve Qwen3.8-Flash-Next (INT8 w8a8) for Harnessix on one TP4 replica.
# Same engine settings as the 2026-10-23 production deployment, but bound to loopback only.
set -e
source /opt/dtk/env.sh
source /opt/hyhal/env.sh
set -euo pipefail

: "${MODEL_DIRECTORY:?}"
: "${PORT:?}"
# Only weights that passed the official manifest SHA256 check may be served.
test -f "/evidence/${MODEL_DIRECTORY}.verified.json"

exec vllm serve "/models/${MODEL_DIRECTORY}" \
  --host 127.0.0.1 --port "${PORT}" \
  --served-model-name qwen3.8-flash-next \
  --dtype bfloat16 --max-num-seqs "${MAX_NUM_SEQS:-32}" \
  --tensor-parallel-size 4 --max-model-len 262144 \
  --max-num-batched-tokens 4096 --gpu-memory-utilization 0.92 \
  --speculative-config '{"method":"mtp","num_speculative_tokens":1}' \
  --enable-auto-tool-choice --tool-call-parser qwen3_xml \
  --reasoning-parser qwen3 --enable-prefix-caching \
  --compilation-config '{"cudagraph_mode":"FULL_AND_PIECEWISE"}'
