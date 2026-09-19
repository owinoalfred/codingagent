#!/usr/bin/env bash
set -euo pipefail

# Serves Qwen3-Coder-Next (AWQ 4-bit) on a single 48GB-class GPU.
#
# Context length is deliberately capped well below the model's native 256K —
# a full 256K KV cache does not fit alongside the quantized weights on a
# single 48GB card. 32K is plenty for per-file edits and moderate repo
# context; raise it if you move to a bigger GPU or add tensor-parallel
# GPUs later (--tensor-parallel-size 2 on 2x48GB cards, etc).

MODEL="${MODEL_NAME:-bullpoint/Qwen3-Coder-Next-AWQ-4bit}"
MAX_LEN="${MAX_MODEL_LEN:-32768}"
GPU_UTIL="${GPU_MEMORY_UTILIZATION:-0.92}"

exec vllm serve "$MODEL" \
    --served-model-name qwen3-coder-next \
    --host 0.0.0.0 \
    --port 8000 \
    --max-model-len "$MAX_LEN" \
    --gpu-memory-utilization "$GPU_UTIL" \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder \
    --reasoning-parser qwen3 \
    --enable-prefix-caching
