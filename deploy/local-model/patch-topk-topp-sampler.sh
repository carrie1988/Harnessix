#!/usr/bin/env bash
# Build the one-line sampler patch mounted by run-flash-replicas.sh.
#
# Root cause (found 2026-11-05 on Hygon K100_AI / DTK 26.04 / vLLM 0.29.0): vLLM switches to the Triton
# `_topk_topp_kernel` once a sampling batch has >= 8 logits rows. With one MTP draft token every request
# yields two rows, so 4 concurrent sampled requests reach the threshold and the kernel hangs the engine
# (VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS expires and EngineCore dies). The patch keeps ROCm/DCU on the
# PyTorch sort path. Greedy requests never call this function, which is why earlier deployments that only
# used temperature 0 did not see the hang.
set -euo pipefail

IMAGE="harbor.sourcefind.cn:5443/dcu/admin/base/custom:vllm0.29.0-ubuntu22.04-dtk26.04-py3.10-20260831-qwen3.8flashnext"
SOURCE=/usr/local/lib/python3.10/dist-packages/vllm/v1/sample/ops/topk_topp_sampler.py
OUT_DIR=/data/harnessix/serving/patches
mkdir -p "$OUT_DIR"

docker run --rm --entrypoint cat "$IMAGE" "$SOURCE" > "$OUT_DIR/topk_topp_sampler.py.orig"
python3 - "$OUT_DIR/topk_topp_sampler.py.orig" "$OUT_DIR/topk_topp_sampler.py" <<'EOF'
import sys

original, patched = sys.argv[1], sys.argv[2]
text = open(original).read()
old = "    if HAS_TRITON and logits.shape[0] >= 8:\n"
new = (
    "    # Harnessix: the Triton top-k/top-p kernel hangs on Hygon DCU (ROCm/gfx928) once a batch\n"
    "    # reaches 8 logits rows (4 requests with 1 MTP draft token each).\n"
    "    if HAS_TRITON and logits.shape[0] >= 8 and not current_platform.is_rocm():\n"
)
if text.count(old) != 1:
    raise SystemExit("unexpected sampler source; review the patch for this vLLM build")
open(patched, "w").write(text.replace(old, new))
EOF
echo "patched sampler written to $OUT_DIR/topk_topp_sampler.py"
