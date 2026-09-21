import sys
import os
import shutil
import asyncio
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agent-api"))

import db
import approval
from git_worktree import setup_task_worktree, cleanup_task_worktree
from agents import planner, coder, tester, debugger, reviewer, security
import orchestrator

def setup_e2e_toy_repo(repo_dir: Path):
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)

    calc_py = """def add(a, b):
    return a + b

def divide(a, b):
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
"""
    test_calc_py = """from calculator import add, divide
import pytest

def test_add():
    assert add(2, 3) == 5

def test_divide():
    assert divide(6, 2) == 3
    with pytest.raises(ValueError):
        divide(5, 0)
"""
    readme_md = "# Calculator Library\nSimple python calculator."

    (repo_dir / "calculator.py").write_text(calc_py)
    (repo_dir / "test_calculator.py").write_text(test_calc_py)
    (repo_dir / "README.md").write_text(readme_md)

    # Git init
    os.system(f"git -C {repo_dir} init")
    os.system(f"git -C {repo_dir} config user.name 'Agent'")
    os.system(f"git -C {repo_dir} config user.email 'agent@local.dev'")
    os.system(f"git -C {repo_dir} add -A")
    os.system(f"git -C {repo_dir} commit -m 'Initial calculator commit'")

@pytest.mark.asyncio
async def test_e2e_full_success_flow():
    repo_dir = Path("./workspace_e2e").resolve()
    setup_e2e_toy_repo(repo_dir)

    task_id = db.create_task("Add division to calculator", "Implement division and tests")

    async def mock_smart_runner(task_id: str, agent_module, user_prompt: str, workspace_dir: Path):
        run_id = db.create_agent_run(task_id, agent_module.ROLE)
        db.log_model_usage(task_id, run_id, "qwen3-coder-next", 200, 100, 0.0004)

        if agent_module.ROLE == "planner":
            res = "Plan: Update calculator.py and test_calculator.py"
        elif agent_module.ROLE == "coder":
            # Simulate tool approval
            appr_id = db.request_approval(task_id, run_id, "write_file", {"path": "calculator.py", "content": "..."}, approval.MODERATE)
            db.resolve_approval(appr_id, "APPROVED")
            res = "Implemented divide function."
        elif agent_module.ROLE == "tester":
            res = "PASS: All 2 tests passed."
        elif agent_module.ROLE == "security":
            res = "PASSED: No security issues found."
        elif agent_module.ROLE == "reviewer":
            res = "APPROVED: Changes look clean."
        else:
            res = "Done"

        db.update_agent_run_status(run_id, "COMPLETED")
        return {"status": "COMPLETED", "result": res}

    orch = orchestrator.Orchestrator(repo_dir, mock_smart_runner)
    res = await orch.execute_task(task_id)

    assert res["status"] == "COMPLETED"
    assert "worktree_path" in res
    assert "branch_name" in res

    # Verify task state in database
    task_row = db.get_task(task_id)
    assert task_row["status"] == "COMPLETED"

    # Cleanup worktree and toy repo
    worktree_p = Path(res["worktree_path"])
    cleanup_task_worktree(repo_dir, worktree_p, res["branch_name"], remove_branch=True)
    shutil.rmtree(repo_dir, ignore_errors=True)

@pytest.mark.asyncio
async def test_failure_scenario_debug_exhaustion():
    repo_dir = Path("./workspace_exhaust_test").resolve()
    setup_e2e_toy_repo(repo_dir)

    task_id = db.create_task("Failing Task", "Unfixable bug")

    async def failing_runner(task_id: str, agent_module, user_prompt: str, workspace_dir: Path):
        run_id = db.create_agent_run(task_id, agent_module.ROLE)
        if agent_module.ROLE == "tester":
            res = "FAIL: pytest failed with 3 errors"
        else:
            res = "Attempted debug fix"
        db.update_agent_run_status(run_id, "COMPLETED")
        return {"status": "COMPLETED", "result": res}

    orch = orchestrator.Orchestrator(repo_dir, failing_runner)
    res = await orch.execute_task(task_id)

    assert res["status"] == "REVIEW_REQUIRED"
    assert "exhausted" in res["reason"]

    shutil.rmtree(repo_dir, ignore_errors=True)

@pytest.mark.asyncio
async def test_failure_scenario_security_flagged():
    repo_dir = Path("./workspace_sec_test").resolve()
    setup_e2e_toy_repo(repo_dir)

    task_id = db.create_task("Secret Exposure Task", "Hardcoded credentials")

    async def sec_runner(task_id: str, agent_module, user_prompt: str, workspace_dir: Path):
        run_id = db.create_agent_run(task_id, agent_module.ROLE)
        if agent_module.ROLE == "security":
            res = "FLAGGED: Found hardcoded API key in config.py"
        elif agent_module.ROLE == "tester":
            res = "PASS: All tests passed."
        else:
            res = "OK"
        db.update_agent_run_status(run_id, "COMPLETED")
        return {"status": "COMPLETED", "result": res}

    orch = orchestrator.Orchestrator(repo_dir, sec_runner)
    res = await orch.execute_task(task_id)

    assert res["status"] == "REVIEW_REQUIRED"
    assert "Security review flagged" in res["reason"]

    shutil.rmtree(repo_dir, ignore_errors=True)
