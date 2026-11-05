#!/usr/bin/env bash
# Start the two Harnessix Flash-Next replicas on 247 (cards 0-3 -> :18002, cards 4-7 -> :18001).
# Port 18000 is published by another team's container on this host; do not use it.
# Idempotent: an existing container with the same name is left untouched.
set -euo pipefail

IMAGE="harbor.sourcefind.cn:5443/dcu/admin/base/custom:vllm0.29.0-ubuntu22.04-dtk26.04-py3.10-20260831-qwen3.8flashnext"
MODEL="Qwen3.8-Flash-Next-Channel-INT8-w8a8"
LEGACY=/data/local-model-services-20261023      # TTPs deployment: patches, MoE configs, weight evidence (read-only)
SERVING=/data/harnessix/serving    # holds start-flash.sh (copied from this directory) and per-replica caches
PATCHES="$LEGACY/sources/patches"
VLLM=/usr/local/lib/python3.10/dist-packages/vllm

start_replica() {
  local name=$1 cards=$2 port=$3
  if docker container inspect "$name" >/dev/null 2>&1; then
    echo "$name already exists ($(docker inspect --format '{{.State.Status}}' "$name"))"
    return
  fi
  mkdir -p "$SERVING/cache/$name"
  docker run -d --name "$name" --label owner=harnessix --label harnessix.role=flash-replica \
    --network host --shm-size 64g \
    --device /dev/kfd --device /dev/dri --device /dev/mkfd --group-add 44 --group-add 109 \
    --security-opt seccomp=unconfined --cap-add SYS_NICE --cap-add SYS_PTRACE \
    -e HIP_VISIBLE_DEVICES="$cards" -e PORT="$port" -e MODEL_DIRECTORY="$MODEL" \
    -e MTP_DRAFT_TOKENS="${MTP_DRAFT_TOKENS:-1}" -e PROFILER_CONFIG="${PROFILER_CONFIG:-}" \
    -e VLLM_NUMA_BIND=1 -e VLLM_TORCH_RESHAPE_CACHE=1 \
    -e VLLM_ENABLE_FLA_PACKED_RECURRENT_DECODE=0 -e VLLM_TUNED_CONFIG_FOLDER=/moe_configs \
    -e VLLM_SKIP_WARMUP=1 -e OMP_NUM_THREADS=8 -e PYTHONFAULTHANDLER=1 \
    -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
    -e VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=1800 \
    -v "/data/models/$MODEL:/models/$MODEL:ro" \
    -v "$LEGACY/evidence:/evidence:ro" \
    -v "$LEGACY/sources/configs:/moe_configs:ro" \
    -v /opt/hyhal:/opt/hyhal:ro \
    -v "$SERVING/start-flash.sh:/serving/start-flash.sh:ro" \
    -v "$SERVING/cache/$name:/root/.cache" \
    -v "$PATCHES/moe_align_block_size.py:$VLLM/model_executor/layers/fused_moe/moe_align_block_size.py:ro" \
    -v "$PATCHES/fused_topk_router.py:$VLLM/model_executor/layers/fused_moe/router/fused_topk_router.py:ro" \
    -v "$PATCHES/fused_moe.py:$VLLM/model_executor/layers/fused_moe/fused_moe.py:ro" \
    -v "$PATCHES/_custom_ops.py:$VLLM/_custom_ops.py:ro" \
    -v "$PATCHES/ct_w8a8_int8.py:$VLLM/model_executor/layers/quantization/compressed_tensors/schemes/compressed_tensors_w8a8_int8.py:ro" \
    -v "$PATCHES/model.py:$VLLM/models/qwen4_exp/amd/model.py:ro" \
    -v "$PATCHES/ple_layer.py:$VLLM/models/qwen4_exp/amd/ple_layer.py:ro" \
    -v "$PATCHES/warmup.py:$VLLM/v1/worker/gpu/warmup.py:ro" \
    -v "$SERVING/patches/topk_topp_sampler.py:$VLLM/v1/sample/ops/topk_topp_sampler.py:ro" \
    --entrypoint /bin/bash "$IMAGE" /serving/start-flash.sh >/dev/null
  echo "$name started on cards $cards -> 127.0.0.1:$port"
}

test -f "$LEGACY/evidence/$MODEL.verified.json"
test -f "$SERVING/patches/topk_topp_sampler.py" || { echo "run patch-topk-topp-sampler.sh first" >&2; exit 1; }
# 可只启动一个副本：run-flash-replicas.sh a|b（默认两个）。
case "${1:-all}" in
  a) start_replica harnessix-flash-a 0,1,2,3 18002 ;;
  b) start_replica harnessix-flash-b 4,5,6,7 18001 ;;
  all)
    start_replica harnessix-flash-a 0,1,2,3 18002
    start_replica harnessix-flash-b 4,5,6,7 18001
    ;;
  *) echo "usage: $0 [a|b|all]" >&2; exit 2 ;;
esac
