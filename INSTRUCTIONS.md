# Runbook & System Instructions

This document provides instructions for setup, execution, verification, and troubleshooting of the Personal AI Software Engineer system.

---

## System Overview

The Personal AI Software Engineer is an autonomous software engineering agent system powered by self-hosted open-weights models (e.g. Qwen3-Coder-Next served via vLLM on a rented GPU Pod).

Key Architecture Components:
- **Agent API Gateway (`agent-api/app.py`)**: OpenAI-compatible FastAPI gateway.
- **SQLite Task Memory (`agent-api/db.py`)**: Persistent store for projects, tasks, agent runs, messages, approvals, model usage, and decisions.
- **Agent Roles (`agent-api/agents/`)**: Planner, Coder, Tester, Debugger, Security, and Reviewer.
- **Git Worktree Isolation (`agent-api/git_worktree.py`)**: Per-task isolated worktrees (`task-<id>`) and branches (`agent/<id>`).
- **Approval Gates (`agent-api/approval.py`)**: SAFE auto-run, MODERATE/DANGEROUS execution pause & approve flow.
- **Docker Sandbox (`agent-api/tools.py`)**: Disposable Docker container execution mounted to task worktree.
- **Model Router (`agent-api/model_router.py`)**: Task complexity-based routing to cheap/standard model tiers with hard cost ceilings.
- **Observability Dashboard (`/v1/dashboard`)**: Real-time task, token, and cost tracking.

---

## Quick Start & Setup

### 1. Model Server Setup (RunPod / vLLM)
```bash
# Serve Qwen3-Coder-Next AWQ on a single 48GB GPU (e.g., L40S, A6000)
bash vllm/serve.sh
```

### 2. Gateway API Setup
```bash
cd agent-api
pip install -r requirements.txt

# Start Gateway Server
python3 -m uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

---

## Phase Details & Verification Runbook

### Phase 2 — Task Memory, Agent Roles, Git Isolation, Approval Flow
- **Built**: SQLite storage (`db.py`), 5 core agent roles, git worktree isolation, approval tiers (`SAFE`, `MODERATE`, `DANGEROUS`), approval endpoints (`/v1/approvals/*`).
- **Verification Command**:
  ```bash
  python3 test_phase2.py
  ```

### Phase 3 — Docker Sandbox, Debug Loop, Security Agent
- **Built**: Docker container execution sandbox in `tools.py` (enabled via `USE_DOCKER_SANDBOX=true`), 5-attempt debug loop cap in `orchestrator.py`, read-only `Security` agent.
- **Environment Flags**: `USE_DOCKER_SANDBOX=true`, `MAX_DEBUG_ATTEMPTS=5`.
- **Verification Command**:
  ```bash
  python3 test_phase3.py
  ```

### Phase 4 — Model Router, Cheap Tier, Cost Ceiling
- **Built**: `model_router.py` (routes simple doc/typo tasks to cheap tier), per-task cost ceiling USD limit in `orchestrator.py`.
- **Environment Flags**: `PER_TASK_COST_CEILING_USD=2.00`, `CHEAP_MODEL_NAME=qwen2.5-coder-7b`.
- **Verification Command**:
  ```bash
  python3 test_phase4.py
  ```

### Phase 5 — Observability Dashboard
- **Built**: `/v1/dashboard` rendering real-time task states, pending approvals, token counts, and USD cost.
- **Access**: Open `http://localhost:8080/v1/dashboard`
- **Verification Command**:
  ```bash
  python3 test_phase5.py
  ```

---

## Phase 6 — VS Code Integration Polish & Extension Setup

### Connecting Cline (or OpenAI-Compatible VS Code Extension)

To integrate VS Code with your gateway:
1. Install **Cline** (or any OpenAI API-compatible extension in VS Code).
2. Configure Provider settings in VS Code:
   - **API Provider**: `OpenAI Compatible`
   - **Base URL**: `http://localhost:8080/v1`
   - **API Key**: `not-needed` (or any dummy string)
   - **Model ID**: `qwen3-coder-next`

### Approval Flow Behavior & Known Limitations
When Cline issues an objective that requires `MODERATE` or `DANGEROUS` tool execution:
1. The gateway writes an approval request to SQLite (`approvals` table) and returns status `WAITING` with an `approval_id`.
2. **Editor Limitation**: Standard VS Code extensions like Cline expect synchronous tool completion or direct model response streaming; they do not natively support pausing mid-stream for external approval endpoints.
3. **Handling Approvals**:
   - To inspect pending approvals: `GET http://localhost:8080/v1/approvals/pending`
   - To approve: `POST http://localhost:8080/v1/approvals/{approval_id}/approve`
   - To reject: `POST http://localhost:8080/v1/approvals/{approval_id}/reject`
   - **Trusted Execution Mode**: When using VS Code interactively where human prompt supervision is already active, you can pass `"auto_approve": true` in requests or set trusted session mode to avoid pausing on `MODERATE` tool execution.

---

## Troubleshooting Guide

| Issue | What to Check | Solution |
|---|---|---|
| Model endpoint unreachable | Gateway health check `/v1/health` or vLLM server logs | Ensure vLLM container/pod is running on port 8000 and `MODEL_ENDPOINT` in `.env` is correct. |
| Task stuck in `WAITING` status | SQLite `approvals` table (`SELECT * FROM approvals WHERE status = 'PENDING'`) | Approve or reject the pending approval ID via `/v1/approvals/{id}/approve`. |
| Task halted with `REVIEW_REQUIRED` | Task row status in `tasks` table & orchestrator output | Check if cost ceiling was hit (`PER_TASK_COST_CEILING_USD`), debug attempts were exhausted (5 attempts), or security agent flagged hardcoded secrets. |
| Docker Sandbox errors | Docker daemon status / permissions | Set `USE_DOCKER_SANDBOX=false` to fallback to local process execution or ensure Docker daemon is running. |
