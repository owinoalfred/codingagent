"""
Git worktree helper for task isolation.
Creates isolated worktree per task and handles cleanup.
"""
import os
import subprocess
from pathlib import Path
from typing import Tuple, Optional

def setup_task_worktree(repo_dir: Path, task_id: str) -> Tuple[Path, str]:
    """
    Creates an isolated worktree for a task: git worktree add ../task-<id> -b agent/<id>
    Returns (worktree_path, branch_name).
    """
    repo_dir = repo_dir.resolve()
    short_id = task_id[:8]
    branch_name = f"agent/{short_id}"
    worktree_name = f"task-{short_id}"
    worktree_path = repo_dir.parent / worktree_name

    # Check if worktree directory already exists
    if worktree_path.exists():
        return worktree_path, branch_name

    # Ensure repo is a git repo; if not initialized, initialize it
    git_dir = repo_dir / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        # Make initial commit if empty
        subprocess.run(["git", "config", "user.name", "Agent"], cwd=repo_dir, check=False)
        subprocess.run(["git", "config", "user.email", "agent@local.dev"], cwd=repo_dir, check=False)
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=False)
        subprocess.run(["git", "commit", "-m", "Initial commit", "--allow-empty"], cwd=repo_dir, check=False)

    # Check if branch exists
    check_branch = subprocess.run(["git", "rev-parse", "--verify", branch_name], cwd=repo_dir, capture_output=True, text=True)
    if check_branch.returncode == 0:
        # Branch exists, attach worktree without -b
        cmd = ["git", "worktree", "add", str(worktree_path), branch_name]
    else:
        cmd = ["git", "worktree", "add", "-b", branch_name, str(worktree_path)]

    res = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
    if res.returncode != 0:
        # Fallback: create directory directly if worktree fails (e.g. nested worktree or non-commit repo)
        worktree_path.mkdir(parents=True, exist_ok=True)

    return worktree_path, branch_name

def cleanup_task_worktree(repo_dir: Path, worktree_path: Path, branch_name: Optional[str] = None, remove_branch: bool = False):
    """
    Removes worktree and optionally branch.
    """
    repo_dir = repo_dir.resolve()
    worktree_path = worktree_path.resolve()

    if worktree_path.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo_dir, capture_output=True)
        if worktree_path.exists():
            import shutil
            shutil.rmtree(worktree_path, ignore_errors=True)

    if remove_branch and branch_name:
        subprocess.run(["git", "branch", "-D", branch_name], cwd=repo_dir, capture_output=True)
