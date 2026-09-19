# Personal AI Software Engineer — Self-Hosted Qwen3-Coder-Next

Your own model, your own compute, no per-token API bill to a third party.
This repo is Phase 1: a working `VS Code → your API → your model` loop.

## The model

**Qwen3-Coder-Next** — 80B total parameters, MoE, ~3B active per token.
70.6% on SWE-Bench Verified (best open-weight score at release). Meaningfully
below frontier closed models (Kimi K3 ~93%, DeepSeek-V4-Pro ~80%), but it's
yours, it's usable as an always-on coding agent, and it fits on a single
rented GPU instead of a cluster.

## The quantization decision (this is the lever that controls your cost)

| Format | VRAM needed | GPU that fits | Quality |
|---|---|---|---|
| FP8 (full) | ~92 GB weights + KV cache → ~110-140GB total | H200 141GB only | Best |
| AWQ 4-bit | ~40-48 GB total (weights + modest KV cache) | Single 48GB card (L40S, A6000, RTX 6000 Ada) or A100 80GB | Slightly reduced, still solid for coding |

**Recommendation: start with AWQ 4-bit.** The cost difference between a
48GB-class GPU and an H200 is roughly 4-6x per hour. You want to validate
the whole pipeline (agent → tools → tests) before paying for the bigger card.
You can swap the served model string later with zero code changes if you
decide FP8 quality is worth it.

Model used in the configs below: `bullpoint/Qwen3-Coder-Next-AWQ-4bit`
(published on Hugging Face, quantized from `Qwen/Qwen3-Coder-Next`).

## Hosting decision: RunPod, but not "serverless" on day one

True scale-to-zero serverless works great for stateless, short requests.
An agentic coding session is bursty but long-lived (multi-minute tool loops),
and cold-starting an 80B MoE model on every idle gap adds painful latency.

**Recommendation for v0.1: a RunPod On-Demand GPU Pod that you start before
a work session and stop when you're done**, not a pod left running 24/7.
This is the single biggest cost lever you control manually:

```
Running only while you work (~4 hrs/day, 20 days/month):
  48GB-class GPU (~$0.79/hr on RunPod community cloud) × 80 hrs ≈ $63/month

Left running 24/7:
  $0.79/hr × 730 hrs ≈ $577/month  ← don't do this
```

Once the pipeline is proven, migrate to RunPod Serverless with a min-workers
setting if you want it always-warm, or accept cold starts if you don't mind
a ~20-40s wait on the first request after idle.

## What's in this repo

```
personal-ai-engineer/
├── vllm/
│   ├── Dockerfile        # vLLM server image serving Qwen3-Coder-Next AWQ
│   └── serve.sh          # the exact vllm serve command + flags
├── agent-api/
│   ├── requirements.txt
│   ├── app.py            # FastAPI gateway — /v1/agent/run
│   └── tools.py          # filesystem, terminal, git tools the model can call
├── docker-compose.yml    # runs the gateway locally, pointed at your RunPod endpoint
└── .env.example
```

## Setup steps

### 1. Stand up the model server on RunPod

1. Create a RunPod account, add a payment method.
2. Deploy a **GPU Pod** (not serverless yet) — community cloud, single GPU,
   48GB VRAM class (L40S, A6000, or RTX 6000 Ada). Use the `runpod/pytorch`
   base template or build the image in `vllm/Dockerfile`.
3. Expose port `8000` (RunPod gives you a public URL + port mapping, or use
   their built-in proxy).
4. SSH in (or use their web terminal) and run `vllm/serve.sh`, or build and
   push `vllm/Dockerfile` and run it as the pod's container.
5. Confirm it's up:
   ```bash
   curl http://<runpod-ip>:8000/v1/models
   ```
6. **Stop the pod when you're not using it.** RunPod bills by the second
   while running; stopping (not just closing your terminal) is what stops
   the meter.

### 2. Configure and run the agent gateway (local, on your machine)

```bash
cd agent-api
cp ../.env.example ../.env
# edit .env: set MODEL_ENDPOINT to your RunPod URL, set WORKSPACE_DIR
pip install -r requirements.txt
uvicorn app:app --reload --port 8080
```

### 3. Point VS Code at it

Use Cline (or any OpenAI-compatible VS Code agent extension) with:
- Base URL: `http://localhost:8080/v1`
- Model name: `qwen3-coder-next`
- API key: anything (your gateway doesn't require one yet — add auth before
  exposing this beyond localhost)

### 4. Verify the loop end to end

```bash
curl -X POST http://localhost:8080/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{"task": "List the files in the current project and summarize the structure."}'
```

You should see the gateway call your Qwen3-Coder-Next endpoint, the model
issue a tool call for `list_files`, the gateway execute it, and a final
summary come back.

## Cost reality check (monthly, at your usage)

```
RunPod GPU Pod (48GB, ~4hrs/day, 20 days)   ~$60-90
RunPod GPU Pod idle time you forget to stop  → this is your real risk
Storage (model weights cache, network volume) ~$5-10
TOTAL                                         ~$65-100/month
```

This replaces a variable per-token bill with a flat, predictable one — that
was the actual goal. Quality is lower than Kimi K3 or Opus-class models;
that's the trade you're making for owning the stack end to end.

## What comes after this MVP

This is Phase 1 only — model serving + a minimal agent loop with three tools.
The full architecture (planner/coder/tester/debugger/reviewer agents,
Postgres memory, Docker sandboxing, git worktrees, approval gates) from the
original plan still applies — it now points at your self-hosted endpoint
instead of a paid API. Nothing in that design changes; only `MODEL_ENDPOINT`
does.
