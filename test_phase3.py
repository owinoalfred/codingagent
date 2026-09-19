import sys
import os
import json
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agent-api"))

import db
import approval
from git_worktree import setup_task_worktree, cleanup_task_worktree
from agents import planner, coder, tester, debugger, reviewer, security
import orchestrator

async def mock_debug_runner(task_id: str, agent_module, user_prompt: str, workspace_dir: Path):
    run_id = db.create_agent_run(task_id, agent_module.ROLE)

    # Verify security agent allowed tools enforcement
    if agent_module.ROLE == "security":
        assert getattr(agent_module, "ALLOWED_TOOLS") == ["list_files", "read_file", "git_diff"]
        if "HARDCODED_SECRET" in user_prompt:
            res = "FLAGGED: Found hardcoded secret key in config."
        else:
            res = "PASSED: No vulnerabilities found."
        db.update_agent_run_status(run_id, "COMPLETED")
        return {"status": "COMPLETED", "result": res}

    if agent_module.ROLE == "tester":
        # Check if debug fix applied
        if (workspace_dir / "fixed.txt").exists():
            res = "PASS: All tests passed!"
        else:
            res = "FAIL: AssertionError in test_main.py line 12"
        db.update_agent_run_status(run_id, "COMPLETED")
        return {"status": "COMPLETED", "result": res}

    if agent_module.ROLE == "debugger":
        # Apply fix on 3rd attempt
        attempts = db.get_db().execute("SELECT count(*) as c FROM agent_runs WHERE task_id = ? AND agent_role = 'debugger'", (task_id,)).fetchone()["c"]
        if attempts >= 2:
            (workspace_dir / "fixed.txt").write_text("fixed")
        res = f"Debug fix attempt {attempts}"
        db.update_agent_run_status(run_id, "COMPLETED")
        return {"status": "COMPLETED", "result": res}

    db.update_agent_run_status(run_id, "COMPLETED")
    return {"status": "COMPLETED", "result": "Done"}

async def main():
    print("--- Running Phase 3 Verification ---")

    # Test 1: Debug Loop Recovery
    task_id = db.create_task("Debug Test Task", "Fix failing test")
    repo_dir = Path("./workspace_toy_p3").resolve()
    repo_dir.mkdir(exist_ok=True)
    worktree_path, branch_name = setup_task_worktree(repo_dir, task_id)

    orch = orchestrator.Orchestrator(repo_dir, mock_debug_runner)
    res = await orch.execute_task(task_id)

    print(f"1. Debug recovery status: {res['status']}")
    assert res['status'] == orchestrator.STATUS_COMPLETED
    assert (worktree_path / "fixed.txt").exists()

    # Test 2: Debug Loop Exhaustion
    async def failing_runner(task_id: str, agent_module, user_prompt: str, workspace_dir: Path):
        run_id = db.create_agent_run(task_id, agent_module.ROLE)
        if agent_module.ROLE == "tester":
            res = "FAIL: Always failing test"
        else:
            res = "Tried fixing"
        db.update_agent_run_status(run_id, "COMPLETED")
        return {"status": "COMPLETED", "result": res}

    task2_id = db.create_task("Exhaustion Test Task", "Never passes")
    worktree_path2, branch_name2 = setup_task_worktree(repo_dir, task2_id)
    orch2 = orchestrator.Orchestrator(repo_dir, failing_runner)
    res2 = await orch2.execute_task(task2_id)

    print(f"2. Exhaustion status: {res2['status']}")
    assert res2['status'] == orchestrator.STATUS_REVIEW_REQUIRED
    assert "exhausted" in res2['reason']

    # Test 3: Security Agent Hardcoded Secret Detection
    async def sec_runner(task_id: str, agent_module, user_prompt: str, workspace_dir: Path):
        run_id = db.create_agent_run(task_id, agent_module.ROLE)
        if agent_module.ROLE == "security":
            res = "FLAGGED: Hardcoded AWS secret key detected in app.py"
        elif agent_module.ROLE == "tester":
            res = "PASS: Tests passed"
        else:
            res = "Ok"
        db.update_agent_run_status(run_id, "COMPLETED")
        return {"status": "COMPLETED", "result": res}

    task3_id = db.create_task("Security Test Task", "Secret leak")
    worktree_path3, branch_name3 = setup_task_worktree(repo_dir, task3_id)
    orch3 = orchestrator.Orchestrator(repo_dir, sec_runner)
    res3 = await orch3.execute_task(task3_id)

    print(f"3. Security flagged status: {res3['status']}")
    assert res3['status'] == orchestrator.STATUS_REVIEW_REQUIRED
    assert "Security" in res3['reason'] or "security" in res3['reason'].lower()

    # Cleanups
    cleanup_task_worktree(repo_dir, worktree_path, branch_name, True)
    cleanup_task_worktree(repo_dir, worktree_path2, branch_name2, True)
    cleanup_task_worktree(repo_dir, worktree_path3, branch_name3, True)

    print("--- Phase 3 Verification PASSED ---")

if __name__ == "__main__":
    asyncio.run(main())
