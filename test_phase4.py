import sys
import os
import json
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agent-api"))

import db
import model_router
import orchestrator

async def main():
    print("--- Running Phase 4 Verification ---")

    # Test 1: Model Routing by Task Complexity
    docstring_messages = [{"role": "user", "content": "Fix docstring typo in utils.py"}]
    provider1 = model_router.get_model_provider("task-1", docstring_messages)
    endpoint1, model1 = provider1.get_endpoint_and_model()
    print(f"1. Low complexity task routed to: {model1}")
    assert isinstance(provider1, model_router.CheapModelProvider)

    complex_messages = [{"role": "user", "content": "Refactor authentication layer with OAuth2"}]
    provider2 = model_router.get_model_provider("task-2", complex_messages)
    endpoint2, model2 = provider2.get_endpoint_and_model()
    print(f"2. High complexity task routed to: {model2}")
    assert isinstance(provider2, model_router.QwenSelfHostedProvider)

    # Test 2: Cost Ceiling Enforcement
    task_id = db.create_task("Cost Ceiling Task", "Exceed cost ceiling")

    # Artificially insert model usage exceeding ceiling ($2.00 default)
    db.log_model_usage(task_id, "run-1", "qwen3-coder-next", 1000000, 500000, 2.50)

    total_cost = db.get_task_total_cost(task_id)
    print(f"3. Task total cost logged: ${total_cost:.2f}")
    assert total_cost >= 2.00

    async def dummy_runner(task_id: str, agent_module, user_prompt: str, workspace_dir: Path):
        return {"status": "COMPLETED", "result": "Done"}

    repo_dir = Path("./workspace_toy_p4").resolve()
    repo_dir.mkdir(exist_ok=True)
    orch = orchestrator.Orchestrator(repo_dir, dummy_runner)

    res = await orch.execute_task(task_id)
    print(f"4. Cost ceiling halted task status: {res['status']}")
    assert res['status'] == orchestrator.STATUS_REVIEW_REQUIRED
    assert "Cost ceiling exceeded" in res['reason']

    print("--- Phase 4 Verification PASSED ---")

if __name__ == "__main__":
    asyncio.run(main())
