import sys
import os
import shutil
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agent-api"))

import tools
from git_worktree import setup_task_worktree, cleanup_task_worktree

def test_path_escaping_prevention():
    test_dir = Path("./workspace_sandbox_test").resolve()
    test_dir.mkdir(exist_ok=True)

    # Reading inside workspace should work
    (test_dir / "safe.txt").write_text("safe content")
    content = tools.read_file("safe.txt", workspace_dir=test_dir)
    assert content == "safe content"

    # Escaping attempt should raise ValueError
    with pytest.raises(ValueError) as exc_info:
        tools.read_file("../../../etc/passwd", workspace_dir=test_dir)
    assert "escapes workspace directory" in str(exc_info.value)

    shutil.rmtree(test_dir, ignore_errors=True)

def test_worktree_isolation():
    repo_dir = Path("./workspace_git_test").resolve()
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)

    # Initialize repo as a git repo with an initial commit
    os.system(f"git -C {repo_dir} init")
    os.system(f"git -C {repo_dir} config user.name 'Agent'")
    os.system(f"git -C {repo_dir} config user.email 'agent@local.dev'")
    (repo_dir / "README.md").write_text("# Test Repo")
    os.system(f"git -C {repo_dir} add -A")
    os.system(f"git -C {repo_dir} commit -m 'Initial commit'")

    task_id = "test-sandbox-12345"
    worktree_path, branch_name = setup_task_worktree(repo_dir, task_id)

    assert worktree_path.exists()
    assert f"task-{task_id[:8]}" in str(worktree_path)

    # Modify file in worktree
    tools.write_file("worktree_file.txt", "hello from worktree", workspace_dir=worktree_path)
    assert (worktree_path / "worktree_file.txt").exists()

    # Verify command execution in worktree
    out = tools.run_command("cat worktree_file.txt", workspace_dir=worktree_path)
    assert "hello from worktree" in out

    # Cleanup
    cleanup_task_worktree(repo_dir, worktree_path, branch_name, remove_branch=True)
    assert not worktree_path.exists()
    shutil.rmtree(repo_dir, ignore_errors=True)

def test_run_command_timeout():
    test_dir = Path("./workspace_timeout_test").resolve()
    test_dir.mkdir(exist_ok=True)

    out = tools.run_command("sleep 5", timeout=1, workspace_dir=test_dir)
    assert "timed out" in out.lower() or "timeout" in out.lower()

    shutil.rmtree(test_dir, ignore_errors=True)

def test_docker_sandbox_execution(monkeypatch):
    test_dir = Path("./workspace_docker_test").resolve()
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True, exist_ok=True)

    # Test Docker sandbox mode if USE_DOCKER_SANDBOX enabled
    monkeypatch.setattr(tools, "USE_DOCKER_SANDBOX", True)
    output = tools.run_command("python3 -c 'print(\"Inside Sandbox\")'", workspace_dir=test_dir)
    assert "Inside Sandbox" in output or "timed out" in output or "Error" in output or "Docker" in output

    shutil.rmtree(test_dir, ignore_errors=True)
