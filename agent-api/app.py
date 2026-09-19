"""
Phase 1 agent gateway.

VS Code (via Cline or any OpenAI-compatible extension) -> this API ->
your self-hosted Qwen3-Coder-Next endpoint (vLLM, on RunPod) -> tools ->
response.

This is intentionally minimal: one task in, a bounded tool-calling loop,
one final answer out. No task queue, no Postgres, no multi-agent split yet
-- those come in Phase 2+ once this loop is proven to work end to end.
"""
import json
import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

from tools import TOOL_IMPLS, TOOL_SCHEMAS

load_dotenv()

MODEL_ENDPOINT = os.getenv("MODEL_ENDPOINT", "http://localhost:8000/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen3-coder-next")
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "10"))

SYSTEM_PROMPT = """You are a software engineering agent with access to tools
for reading/writing files, running shell commands, and using git, all
scoped to a single workspace directory. Investigate before you change
anything. Run tests after making changes when a test command is available.
Explain what you did and why in your final answer."""

app = FastAPI(title="Personal AI Engineer - Agent Gateway")


class AgentRunRequest(BaseModel):
    task: str


class AgentRunResponse(BaseModel):
    result: str
    tool_calls_made: int
    raw_messages: list


async def call_model(client: httpx.AsyncClient, messages: list) -> dict:
    resp = await client.post(
        f"{MODEL_ENDPOINT}/chat/completions",
        json={
            "model": MODEL_NAME,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "temperature": 0.2,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()


@app.post("/v1/agent/run", response_model=AgentRunResponse)
async def agent_run(req: AgentRunRequest):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": req.task},
    ]

    tool_calls_made = 0

    async with httpx.AsyncClient() as client:
        for _ in range(MAX_TOOL_ITERATIONS):
            data = await call_model(client, messages)
            choice = data["choices"][0]
            message = choice["message"]
            messages.append(message)

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                # Model gave a final answer -- no more tool calls requested.
                return AgentRunResponse(
                    result=message.get("content", ""),
                    tool_calls_made=tool_calls_made,
                    raw_messages=messages,
                )

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                impl = TOOL_IMPLS.get(fn_name)
                if impl is None:
                    tool_result = f"Unknown tool: {fn_name}"
                else:
                    try:
                        tool_result = impl(**fn_args)
                    except Exception as e:  # noqa: BLE001 - surface tool errors to the model
                        tool_result = f"Tool error: {e}"

                tool_calls_made += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(tool_result),
                    }
                )

    return AgentRunResponse(
        result="Stopped: reached MAX_TOOL_ITERATIONS without a final answer.",
        tool_calls_made=tool_calls_made,
        raw_messages=messages,
    )


@app.get("/v1/health")
async def health():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{MODEL_ENDPOINT}/models", timeout=10.0)
            model_ok = resp.status_code == 200
        except Exception:
            model_ok = False
    return {"gateway": "ok", "model_endpoint_reachable": model_ok}
