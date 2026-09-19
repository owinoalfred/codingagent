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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import approval
import dashboard_ui
from tools import TOOL_IMPLS, get_tool_schemas_for_agent

load_dotenv()

MODEL_ENDPOINT = os.getenv("MODEL_ENDPOINT", "http://localhost:8000/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen3-coder-next")
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "10"))
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
WORKSPACE_ROOT.mkdir(exist_ok=True)

app = FastAPI(title="Personal AI Engineer - Agent Gateway")
app.mount("/v1/dashboard/assets", StaticFiles(directory=Path(__file__).parent / "static" / "dashboard"), name="dashboard-assets")

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
async def dashboard(view: str = dashboard_ui.DEFAULT_VIEW):
    data = dashboard_ui.collect_dashboard_data()
    model_reachable = await dashboard_ui.model_endpoint_reachable()
    return HTMLResponse(
        content=dashboard_ui.render_dashboard(view, data, model_reachable),
        status_code=200,
    )

@app.get("/v1/health")
async def health():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{MODEL_ENDPOINT}/models", timeout=5.0)
            model_ok = resp.status_code == 200
        except Exception:
            model_ok = False
    return {"gateway": "ok", "model_endpoint_reachable": model_ok}
