# Hosting & Deployment Guide — Personal AI Software Engineer

This guide explains **where** to host the model, **what** software to install, **how** to run the agent gateway, and **how** to connect VS Code for daily use.

---

## Architecture Summary — Where Components Run

```
┌─────────────────────────────────────────┐
│              Your Local PC              │
│                                         │
│  [VS Code + Cline]                      │
│        │                                │
│        ▼                                │
│  [Agent Gateway] (localhost:8080)       │
│   ├── SQLite Memory (agent_memory.db)   │
│   ├── Git Worktrees (/task-xyz)         │
│   └── Dashboard (localhost:8080/v1/...) │
└────────────────────┬────────────────────┘
                     │ HTTP API (OpenAI spec)
                     ▼
┌─────────────────────────────────────────┐
│            RunPod GPU Pod               │
│                                         │
│  [vLLM Server] (port 8000)              │
│   └── Model: Qwen3-Coder-Next AWQ 4-bit │
└─────────────────────────────────────────┘
```

- **Model Serving**: Rent a GPU on **RunPod** (On-Demand GPU Pod). Turn it ON when working, OFF when done to minimize cost (~$60–$90/month).
- **Agent Gateway & Memory**: Runs **locally on your PC** (or on a cheap local home server) alongside VS Code.

---

## Step 1: Provisioning the GPU Pod on RunPod

### What to Rent:
- **GPU Class**: Single **48GB VRAM GPU** (e.g. NVIDIA **L40S**, **A6000**, **RTX 6000 Ada**, or **A100 80GB**).
- **Provider**: [RunPod.io](https://www.runpod.io) (Community Cloud or Secure Cloud On-Demand Pod).
- **Disk Space**: At least **60 GB Volume Disk** (for model weights caching).

### Setup Instructions:
1. Log into RunPod and click **Deploy GPU Pod**.
2. Select an **L40S** or **A6000** (48GB VRAM).
3. Select template: `RunPod PyTorch 2.1` or `ubuntu:22.04`.
4. Under **HTTP Ports**, expose port `8000`.
5. Deploy and start the Pod.
6. Connect via **SSH** or open the **RunPod Web Terminal**.

### Starting vLLM on the Pod:
In the pod terminal, clone this repo or run `vllm/serve.sh`:

```bash
# Clone repo on RunPod (or copy vllm/serve.sh)
git clone <your-repo-url> personal-ai-engineer
cd personal-ai-engineer/vllm

# Run vLLM serving script
bash serve.sh
```

*(This starts vLLM on port `8000` loading `bullpoint/Qwen3-Coder-Next-AWQ-4bit`)*

---

## Step 2: Installing & Running the Agent Gateway (On Your Local PC)

### What to Install on Your Local PC:
- **Python**: Version `3.12` or higher.
- **Git**: Installed and configured (`git config --global user.name` / `user.email`).
- **Docker** *(Optional)*: Installed and running if you enable `USE_DOCKER_SANDBOX=true` for containerized command execution.

### Setup Steps:

1. **Clone the repository on your local machine**:
   ```bash
   git clone <your-repo-url> personal-ai-engineer
   cd personal-ai-engineer
   ```

2. **Set up Environment Variables**:
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and set `MODEL_ENDPOINT` to your RunPod public endpoint:
   ```ini
   MODEL_ENDPOINT=http://<YOUR_RUNPOD_IP_OR_PROXY>:8000/v1
   MODEL_NAME=qwen3-coder-next
   WORKSPACE_DIR=./workspace
   DATABASE_PATH=agent_memory.db
   PER_TASK_COST_CEILING_USD=2.00
   USE_DOCKER_SANDBOX=false
   ```

3. **Install Gateway Dependencies**:
   ```bash
   cd agent-api
   pip install -r requirements.txt
   ```

4. **Start the Agent Gateway**:
   ```bash
   python3 -m uvicorn app:app --host 0.0.0.0 --port 8080 --reload
   ```

   *(You can also use `docker-compose up` if you prefer running the gateway inside Docker)*.

---

## Step 3: Connecting VS Code

### Extension to Install:
- Install **Cline** (or **Continue.dev**, or any OpenAI API-compatible extension) from the VS Code Marketplace.

### Configuration in VS Code (Cline Settings):
- **API Provider**: Select `OpenAI Compatible`
- **Base URL**: `http://localhost:8080/v1`
- **API Key**: `not-needed` (any string)
- **Model ID**: `qwen3-coder-next`

---

## Step 4: Daily Workflow & Operations

### 1. Starting a Work Session
1. Log into RunPod and **Start** your GPU Pod.
2. Run `bash vllm/serve.sh` inside the RunPod pod.
3. Start the local gateway on your PC: `python3 -m uvicorn app:app --port 8080`.
4. Open VS Code and prompt the system.

### 2. Approving Tool Execution
- When the agent performs a `MODERATE` or `DANGEROUS` operation (e.g. modifying files or executing shell commands), it logs an approval request and pauses in `WAITING` status.
- Open the Observability Dashboard at:
  `http://localhost:8080/v1/dashboard`
- Click **Approve** or **Reject** on pending approval requests (or make a POST request to `http://localhost:8080/v1/approvals/<id>/approve`). The task automatically resumes execution in the background!

### 3. Ending a Work Session
- **STOP your GPU Pod on RunPod when finished!** (RunPod charges per second while running; stopping the pod stops the billing meter).

---

## Troubleshooting Checklist

| Problem | Solution |
|---|---|
| `model_endpoint_reachable: false` on `/v1/health` | Check if vLLM server is running on RunPod port 8000 and `MODEL_ENDPOINT` in `.env` has the correct IP/port. |
| Task paused in `WAITING` status | View pending approvals at `http://localhost:8080/v1/dashboard` and approve via `/v1/approvals/<id>/approve`. |
| Docker sandbox error | Make sure Docker Desktop is running on your machine or set `USE_DOCKER_SANDBOX=false` in `.env` to run processes locally. |
