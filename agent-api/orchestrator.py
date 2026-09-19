"""
Orchestrator: Manages task state machine and multi-agent workflow execution graph.
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Dict, Any, Optional, Callable, List

import db
from git_worktree import setup_task_worktree, cleanup_task_worktree
from agents import planner, coder, tester, debugger, reviewer

# Task States
STATUS_PENDING = "PENDING"
STATUS_PLANNING = "PLANNING"
STATUS_RUNNING = "RUNNING"
STATUS_WAITING = "WAITING"
STATUS_FAILED = "FAILED"
STATUS_RETRYING = "RETRYING"
STATUS_COMPLETED = "COMPLETED"
STATUS_REVIEW_REQUIRED = "REVIEW_REQUIRED"
STATUS_APPROVED = "APPROVED"

MAX_DEBUG_ATTEMPTS = int(os.getenv("MAX_DEBUG_ATTEMPTS", "5"))
PER_TASK_COST_CEILING = float(os.getenv("PER_TASK_COST_CEILING_USD", "2.00"))

class Orchestrator:
    def __init__(self, workspace_root: Path, runner_func: Callable):
        """
        runner_func signature:
        async def run_agent_loop(task_id: str, agent_module, prompt_override: str, workspace_dir: Path) -> dict:
            return {"result": str, "success": bool, "test_passed": bool, ...}
        """
        self.workspace_root = workspace_root.resolve()
        self.runner_func = runner_func

    def check_cost_ceiling(self, task_id: str) -> bool:
        total_cost = db.get_task_total_cost(task_id)
        if total_cost >= PER_TASK_COST_CEILING:
            db.update_task_status(task_id, STATUS_REVIEW_REQUIRED)
            return False
        return True

    async def execute_task(self, task_id: str) -> Dict[str, Any]:
        task = db.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        # 1. Setup isolated git worktree
        worktree_path, branch_name = setup_task_worktree(self.workspace_root, task_id)
        db.update_task_status(task_id, STATUS_PLANNING, worktree_path=str(worktree_path), branch_name=branch_name)

        try:
            # 2. Planning phase
            if not self.check_cost_ceiling(task_id):
                return {"status": STATUS_REVIEW_REQUIRED, "reason": "Cost ceiling exceeded before planning"}

            plan_prompt = f"Objective: {task['title']}\nDescription: {task['description'] or ''}"
            plan_res = await self.runner_func(
                task_id=task_id,
                agent_module=planner,
                user_prompt=plan_prompt,
                workspace_dir=worktree_path
            )

            if plan_res.get("status") == STATUS_WAITING:
                return {"status": STATUS_WAITING, "task_id": task_id, "approval_id": plan_res.get("approval_id")}

            db.update_task_status(task_id, STATUS_RUNNING)

            # 3. Coding phase
            if not self.check_cost_ceiling(task_id):
                return {"status": STATUS_REVIEW_REQUIRED, "reason": "Cost ceiling exceeded before coding"}

            code_prompt = f"Plan and Objective:\n{plan_res.get('result', '')}\nImplement the solution in full."
            code_res = await self.runner_func(
                task_id=task_id,
                agent_module=coder,
                user_prompt=code_prompt,
                workspace_dir=worktree_path
            )

            if code_res.get("status") == STATUS_WAITING:
                return {"status": STATUS_WAITING, "task_id": task_id, "approval_id": code_res.get("approval_id")}

            # 4. Testing phase
            if not self.check_cost_ceiling(task_id):
                return {"status": STATUS_REVIEW_REQUIRED, "reason": "Cost ceiling exceeded before testing"}

            test_prompt = "Run all relevant tests in the workspace and report results."
            test_res = await self.runner_func(
                task_id=task_id,
                agent_module=tester,
                user_prompt=test_prompt,
                workspace_dir=worktree_path
            )

            if test_res.get("status") == STATUS_WAITING:
                return {"status": STATUS_WAITING, "task_id": task_id, "approval_id": test_res.get("approval_id")}

            # 5. Debug loop if tests failed
            debug_attempts = 0
            test_output = test_res.get("result", "")

            # Check if test failed
            is_failure = "FAIL" in test_output.upper() or "ERROR" in test_output.upper() or "FAILED" in test_output.upper()

            while is_failure and debug_attempts < MAX_DEBUG_ATTEMPTS:
                if not self.check_cost_ceiling(task_id):
                    return {"status": STATUS_REVIEW_REQUIRED, "reason": "Cost ceiling exceeded during debugging"}

                debug_attempts += 1
                db.update_task_status(task_id, STATUS_RETRYING)

                debug_prompt = f"Test Failure Output (Attempt {debug_attempts}/{MAX_DEBUG_ATTEMPTS}):\n{test_output}\nLocate bug and apply fix."
                debug_res = await self.runner_func(
                    task_id=task_id,
                    agent_module=debugger,
                    user_prompt=debug_prompt,
                    workspace_dir=worktree_path
                )

                if debug_res.get("status") == STATUS_WAITING:
                    return {"status": STATUS_WAITING, "task_id": task_id, "approval_id": debug_res.get("approval_id")}

                # Re-test
                test_res = await self.runner_func(
                    task_id=task_id,
                    agent_module=tester,
                    user_prompt="Re-run tests to verify debugger fix.",
                    workspace_dir=worktree_path
                )

                if test_res.get("status") == STATUS_WAITING:
                    return {"status": STATUS_WAITING, "task_id": task_id, "approval_id": test_res.get("approval_id")}

                test_output = test_res.get("result", "")
                is_failure = "FAIL" in test_output.upper() or "ERROR" in test_output.upper() or "FAILED" in test_output.upper()

            if is_failure and debug_attempts >= MAX_DEBUG_ATTEMPTS:
                db.update_task_status(task_id, STATUS_REVIEW_REQUIRED)
                return {
                    "status": STATUS_REVIEW_REQUIRED,
                    "reason": f"Debug loop exhausted after {MAX_DEBUG_ATTEMPTS} attempts without passing tests.",
                    "test_output": test_output
                }

            # 6. Security & Reviewer Phase
            # Security agent check (will be imported dynamically or called if present)
            try:
                from agents import security
                sec_prompt = "Perform security audit on git diff and code modifications."
                sec_res = await self.runner_func(
                    task_id=task_id,
                    agent_module=security,
                    user_prompt=sec_prompt,
                    workspace_dir=worktree_path
                )
                if sec_res.get("status") == STATUS_WAITING:
                    return {"status": STATUS_WAITING, "task_id": task_id, "approval_id": sec_res.get("approval_id")}

                if "FLAGGED" in sec_res.get("result", "").upper() or "VULNERABILITY" in sec_res.get("result", "").upper() or "SECRET" in sec_res.get("result", "").upper():
                    db.update_task_status(task_id, STATUS_REVIEW_REQUIRED)
                    return {
                        "status": STATUS_REVIEW_REQUIRED,
                        "reason": "Security review flagged potential security issues.",
                        "security_output": sec_res.get("result")
                    }
            except ImportError:
                pass

            # Code Reviewer check
            if not self.check_cost_ceiling(task_id):
                return {"status": STATUS_REVIEW_REQUIRED, "reason": "Cost ceiling exceeded before code review"}

            review_prompt = "Review final git diff and state for task completion."
            review_res = await self.runner_func(
                task_id=task_id,
                agent_module=reviewer,
                user_prompt=review_prompt,
                workspace_dir=worktree_path
            )

            if review_res.get("status") == STATUS_WAITING:
                return {"status": STATUS_WAITING, "task_id": task_id, "approval_id": review_res.get("approval_id")}

            db.update_task_status(task_id, STATUS_COMPLETED)
            return {
                "status": STATUS_COMPLETED,
                "task_id": task_id,
                "worktree_path": str(worktree_path),
                "branch_name": branch_name,
                "result": review_res.get("result")
            }

        except Exception as e:
            db.update_task_status(task_id, STATUS_FAILED)
            return {"status": STATUS_FAILED, "task_id": task_id, "error": str(e)}
