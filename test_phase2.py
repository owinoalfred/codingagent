import sys
import os
import json
import asyncio
from pathlib import Path

# Add agent-api to python path
sys.path.insert(0, str(Path(__file__).parent / "agent-api"))

import db
import approval
from git_worktree import setup_task_worktree, cleanup_task_worktree
from agents import planner, coder, tester, debugger, reviewer
import orchestrator

async def mock_runner(task_id: str, agent_module, user_prompt: str, workspace_dir: Path):
    run_id = db.create_agent_run(task_id, agent_module.ROLE)
    db.log_message(run_id, "system", agent_module.SYSTEM_PROMPT)
    db.log_message(run_id, "user", user_prompt)

    # Log mock model usage
    db.log_model_usage(task_id, run_id, "mock-qwen3-coder-next", 100, 50, 0.0002)

    if agent_module.ROLE == "planner":
        result = "1. Create main.py\n2. Add test_main.py"
    elif agent_module.ROLE == "coder":
        # Simulate moderate tool execution: write_file
        appr_id = db.request_approval(task_id, run_id, "write_file", {"path": "main.py", "content": "print('hello')"}, approval.MODERATE)
        # Check approval classification
        assert approval.classify_tool_risk("write_file", {}) == approval.MODERATE
        assert approval.classify_tool_risk("read_file", {}) == approval.SAFE
        assert approval.classify_tool_risk("run_command", {"command": "rm -rf /"}) == approval.DANGEROUS

        # Approve manually
        db.resolve_approval(appr_id, "APPROVED")
        result = "Wrote main.py successfully."
    elif agent_module.ROLE == "tester":
        result = "PASS: All 1 tests passed."
    elif agent_module.ROLE == "reviewer":
        result = "APPROVED: Code quality looks good."
    else:
        result = "Done"

    db.update_agent_run_status(run_id, "COMPLETED")
    return {"status": "COMPLETED", "result": result}

async def main():
    print("--- Running Phase 2 Verification ---")

    # 1. Create task
    task_id = db.create_task("Trivial Task", "Create hello world script")
    print(f"1. Created Task ID: {task_id}")

    # 2. Setup Worktree
    repo_dir = Path("./workspace_toy").resolve()
    repo_dir.mkdir(exist_ok=True)
    worktree_path, branch_name = setup_task_worktree(repo_dir, task_id)
    print(f"2. Worktree created at {worktree_path}, branch {branch_name}")

    # 3. Test Orchestration flow
    orch = orchestrator.Orchestrator(repo_dir, mock_runner)
    res = await orch.execute_task(task_id)

    print(f"3. Orchestrator result: {res['status']}")
    assert res['status'] == orchestrator.STATUS_COMPLETED

    # 4. Confirm database rows created
    task_row = db.get_task(task_id)
    assert task_row['status'] == orchestrator.STATUS_COMPLETED

    conn = db.get_db()
    agent_runs = conn.execute("SELECT * FROM agent_runs WHERE task_id = ?", (task_id,)).fetchall()
    print(f"4. Total agent runs logged: {len(agent_runs)}")
    assert len(agent_runs) >= 4

    approvals = conn.execute("SELECT * FROM approvals WHERE task_id = ?", (task_id,)).fetchall()
    print(f"5. Total approvals logged: {len(approvals)}")
    assert len(approvals) >= 1
    assert approvals[0]['status'] == "APPROVED"
    conn.close()

    # Clean up worktree
    cleanup_task_worktree(repo_dir, worktree_path, branch_name, remove_branch=True)
    print("--- Phase 2 Verification PASSED ---")

if __name__ == "__main__":
    asyncio.run(main())
