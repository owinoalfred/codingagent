"""
FastAPI Gateway & Agent Execution Orchestration.
Integrates task memory DB, approval flow, agent loops, git worktrees, router, and observability dashboard.
"""
import json
import os
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import db
import approval
from tools import TOOL_IMPLS, get_tool_schemas_for_agent

load_dotenv()

MODEL_ENDPOINT = os.getenv("MODEL_ENDPOINT", "http://localhost:8000/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen3-coder-next")
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "10"))
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
WORKSPACE_ROOT.mkdir(exist_ok=True)

app = FastAPI(title="Personal AI Engineer - Agent Gateway")

class AgentRunRequest(BaseModel):
    task: str
    project_id: Optional[str] = None
    auto_approve: bool = False  # If True, bypasses MODERATE approval wait in trust mode

class ApprovalResolveRequest(BaseModel):
    status: str  # APPROVED or REJECTED
    reason: Optional[str] = None

class TaskCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    project_id: Optional[str] = None

async def call_model_routed(client: httpx.AsyncClient, task_id: str, messages: list, tools: list) -> dict:
    try:
        from model_router import get_model_provider
        provider = get_model_provider(task_id, messages)
        endpoint, model_name = provider.get_endpoint_and_model()
    except Exception:
        endpoint, model_name = MODEL_ENDPOINT, MODEL_NAME

    resp = await client.post(
        f"{endpoint}/chat/completions",
        json={
            "model": model_name,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto" if tools else "none",
            "temperature": 0.2,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()

    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cost = (prompt_tokens * 0.000001) + (completion_tokens * 0.000002)
    db.log_model_usage(task_id, None, model_name, prompt_tokens, completion_tokens, cost)

    return data

async def run_agent_loop(
    task_id: str,
    agent_module,
    user_prompt: str,
    workspace_dir: Path,
    auto_approve: bool = False
) -> Dict[str, Any]:
    run_id = db.create_agent_run(task_id, agent_module.ROLE)

    allowed_tools = getattr(agent_module, "ALLOWED_TOOLS", None)
    tool_schemas = get_tool_schemas_for_agent(allowed_tools)

    messages = [
        {"role": "system", "content": agent_module.SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    db.log_message(run_id, "system", agent_module.SYSTEM_PROMPT)
    db.log_message(run_id, "user", user_prompt)

    tool_calls_made = 0

    async with httpx.AsyncClient() as client:
        for _ in range(MAX_TOOL_ITERATIONS):
            total_cost = db.get_task_total_cost(task_id)
            cost_ceiling = float(os.getenv("PER_TASK_COST_CEILING_USD", "2.00"))
            if total_cost >= cost_ceiling:
                db.update_agent_run_status(run_id, "COST_CEILING_EXCEEDED")
                return {
                    "status": "REVIEW_REQUIRED",
                    "result": f"Task cost ceiling (${cost_ceiling}) hit.",
                    "tool_calls_made": tool_calls_made
                }

            data = await call_model_routed(client, task_id, messages, tool_schemas)
            choice = data["choices"][0]
            message = choice["message"]
            messages.append(message)
            db.log_message(run_id, message.get("role", "assistant"), message.get("content"), message.get("tool_calls"))

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                db.update_agent_run_status(run_id, "COMPLETED")
                return {
                    "status": "COMPLETED",
                    "result": message.get("content", ""),
                    "tool_calls_made": tool_calls_made
                }

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                if allowed_tools is not None and fn_name not in allowed_tools:
                    tool_result = f"Error: Agent '{agent_module.ROLE}' is not authorized to use tool '{fn_name}'."
                else:
                    risk_level = approval.classify_tool_risk(fn_name, fn_args)

                    if risk_level in [approval.MODERATE, approval.DANGEROUS] and not auto_approve:
                        approval_id = db.request_approval(task_id, run_id, fn_name, fn_args, risk_level)
                        db.update_task_status(task_id, "WAITING")
                        db.update_agent_run_status(run_id, "WAITING_APPROVAL")
                        return {
                            "status": "WAITING",
                            "approval_id": approval_id,
                            "tool_name": fn_name,
                            "tool_args": fn_args,
                            "risk_level": risk_level,
                            "result": f"Tool '{fn_name}' paused awaiting human approval (Approval ID: {approval_id})."
                        }

                    impl = TOOL_IMPLS.get(fn_name)
                    if impl is None:
                        tool_result = f"Unknown tool: {fn_name}"
                    else:
                        try:
                            if fn_name in ["list_files", "read_file", "write_file", "run_command", "git", "git_diff"]:
                                tool_result = impl(workspace_dir=workspace_dir, **fn_args)
                            else:
                                tool_result = impl(**fn_args)
                        except Exception as e:
                            tool_result = f"Tool error: {e}"

                tool_calls_made += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(tool_result),
                    }
                )
                db.log_message(run_id, "tool", str(tool_result), tool_call_id=tc["id"])

    db.update_agent_run_status(run_id, "MAX_ITERATIONS_REACHED")
    return {
        "status": "COMPLETED",
        "result": "Stopped: reached MAX_TOOL_ITERATIONS without a final answer.",
        "tool_calls_made": tool_calls_made
    }

async def resume_task_execution(task_id: str):
    """
    Background worker that resumes orchestrator loop after approval is granted.
    """
    from orchestrator import Orchestrator

    async def runner_wrapper(tid: str, agent_module, user_prompt: str, workspace_dir: Path):
        return await run_agent_loop(tid, agent_module, user_prompt, workspace_dir, auto_approve=True)

    orch = Orchestrator(WORKSPACE_ROOT, runner_wrapper)
    await orch.execute_task(task_id)

@app.post("/v1/tasks")
async def create_task_endpoint(req: TaskCreateRequest):
    task_id = db.create_task(req.title, req.description, req.project_id)
    return {"task_id": task_id, "status": "PENDING"}

@app.get("/v1/tasks/{task_id}")
async def get_task_endpoint(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.post("/v1/agent/run")
async def agent_run_endpoint(req: AgentRunRequest):
    task_id = db.create_task(title=req.task[:50], description=req.task, project_id=req.project_id)

    from orchestrator import Orchestrator

    async def runner_wrapper(task_id: str, agent_module, user_prompt: str, workspace_dir: Path):
        return await run_agent_loop(task_id, agent_module, user_prompt, workspace_dir, req.auto_approve)

    orch = Orchestrator(WORKSPACE_ROOT, runner_wrapper)
    result = await orch.execute_task(task_id)

    return {
        "task_id": task_id,
        "execution_result": result
    }

@app.get("/v1/approvals/pending")
async def list_pending_approvals():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM approvals WHERE status = 'PENDING'").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/v1/approvals/{approval_id}/approve")
async def approve_tool_call(approval_id: str, background_tasks: BackgroundTasks):
    appr = db.get_approval(approval_id)
    if not appr:
        raise HTTPException(status_code=404, detail="Approval request not found")
    db.resolve_approval(approval_id, "APPROVED")

    task_id = appr["task_id"]
    # Schedule task resume in background
    background_tasks.add_task(resume_task_execution, task_id)
    return {"status": "APPROVED", "approval_id": approval_id, "resuming_task_id": task_id}

@app.post("/v1/approvals/{approval_id}/reject")
async def reject_tool_call(approval_id: str, req: Optional[ApprovalResolveRequest] = None):
    appr = db.get_approval(approval_id)
    if not appr:
        raise HTTPException(status_code=404, detail="Approval request not found")
    reason = req.reason if req else "Rejected by human operator"
    db.resolve_approval(approval_id, "REJECTED", reason=reason)
    db.update_task_status(appr["task_id"], "REVIEW_REQUIRED")
    return {"status": "REJECTED", "approval_id": approval_id, "reason": reason}

@app.get("/v1/dashboard", response_class=HTMLResponse)
async def dashboard():
    conn = db.get_db()

    total_tasks = conn.execute("SELECT COUNT(*) as count FROM tasks").fetchone()["count"]
    completed_tasks = conn.execute("SELECT COUNT(*) as count FROM tasks WHERE status = 'COMPLETED'").fetchone()["count"]
    failed_tasks = conn.execute("SELECT COUNT(*) as count FROM tasks WHERE status IN ('FAILED', 'REVIEW_REQUIRED')").fetchone()["count"]
    awaiting_approval = conn.execute("SELECT COUNT(*) as count FROM approvals WHERE status = 'PENDING'").fetchone()["count"]

    usage = conn.execute("SELECT SUM(prompt_tokens) as p_tokens, SUM(completion_tokens) as c_tokens, SUM(cost_usd) as total_cost FROM model_usage").fetchone()
    prompt_tokens = usage["p_tokens"] or 0
    completion_tokens = usage["c_tokens"] or 0
    total_tokens = prompt_tokens + completion_tokens
    total_cost = usage["total_cost"] or 0.0

    recent_tasks = conn.execute("SELECT id, title, status, created_at FROM tasks ORDER BY created_at DESC LIMIT 10").fetchall()
    conn.close()

    tasks_rows = "".join([
        f"<tr><td>{t['id'][:8]}</td><td>{t['title']}</td><td><span class='badge {t['status']}'>{t['status']}</span></td><td>{t['created_at']}</td></tr>"
        for t in recent_tasks
    ])

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>AI Engineer Dashboard</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 2rem; }}
            h1 {{ color: #38bdf8; margin-bottom: 1.5rem; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
            .card {{ background: #1e293b; padding: 1.5rem; border-radius: 8px; border: 1px solid #334155; }}
            .card .number {{ font-size: 2rem; font-weight: bold; color: #38bdf8; margin-top: 0.5rem; }}
            table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; }}
            th, td {{ padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid #334155; }}
            th {{ background: #334155; color: #94a3b8; font-size: 0.85rem; text-transform: uppercase; }}
            .badge {{ padding: 0.25rem 0.5rem; border-radius: 4px; font-size: 0.8rem; font-weight: bold; }}
            .COMPLETED {{ background: #059669; color: white; }}
            .FAILED, .REVIEW_REQUIRED {{ background: #dc2626; color: white; }}
            .WAITING, .PENDING {{ background: #d97706; color: white; }}
            .RUNNING, .PLANNING {{ background: #2563eb; color: white; }}
        </style>
    </head>
    <body>
        <h1>Personal AI Engineer — Observability Dashboard</h1>
        <div class="grid">
            <div class="card"><div>Total Tasks</div><div class="number">{total_tasks}</div></div>
            <div class="card"><div>Completed</div><div class="number">{completed_tasks}</div></div>
            <div class="card"><div>Failed / Review Required</div><div class="number">{failed_tasks}</div></div>
            <div class="card"><div>Awaiting Approval</div><div class="number">{awaiting_approval}</div></div>
            <div class="card"><div>Total Tokens</div><div class="number">{total_tokens:,}</div></div>
            <div class="card"><div>Total Cost (USD)</div><div class="number">${total_cost:.4f}</div></div>
        </div>

        <h2>Recent Tasks</h2>
        <table>
            <thead>
                <tr><th>Task ID</th><th>Title</th><th>Status</th><th>Created At</th></tr>
            </thead>
            <tbody>
                {tasks_rows if tasks_rows else "<tr><td colspan='4'>No tasks logged yet.</td></tr>"}
            </tbody>
        </table>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)

@app.get("/v1/health")
async def health():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{MODEL_ENDPOINT}/models", timeout=5.0)
            model_ok = resp.status_code == 200
        except Exception:
            model_ok = False
    return {"gateway": "ok", "model_endpoint_reachable": model_ok}
