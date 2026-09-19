"""
Updated tools module.
Supports workspace scoping per task worktree, read-only tools, and Docker container sandbox isolation.
"""
import os
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

DEFAULT_WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
DEFAULT_WORKSPACE_DIR.mkdir(exist_ok=True)

USE_DOCKER_SANDBOX = os.getenv("USE_DOCKER_SANDBOX", "false").lower() == "true"
DOCKER_IMAGE = os.getenv("DOCKER_SANDBOX_IMAGE", "python:3.12-slim")

def _safe_path(rel_path: str, workspace_dir: Optional[Path] = None) -> Path:
    base = workspace_dir.resolve() if workspace_dir else DEFAULT_WORKSPACE_DIR
    target = (base / rel_path).resolve()
    if base not in target.parents and target != base:
        raise ValueError(f"Path '{rel_path}' escapes workspace directory.")
    return target

def list_files(path: str = ".", workspace_dir: Optional[Path] = None) -> str:
    target = _safe_path(path, workspace_dir)
    if not target.exists():
        return f"Path does not exist: {path}"
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return "\n".join(entries) if entries else "(empty directory)"

def read_file(path: str, workspace_dir: Optional[Path] = None) -> str:
    target = _safe_path(path, workspace_dir)
    if not target.exists():
        return f"File does not exist: {path}"
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"File is not text-decodable: {path}"

def write_file(path: str, content: str, workspace_dir: Optional[Path] = None) -> str:
    target = _safe_path(path, workspace_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} bytes to {path}"

def run_command(command: str, timeout: int = 60, workspace_dir: Optional[Path] = None) -> str:
    base = workspace_dir.resolve() if workspace_dir else DEFAULT_WORKSPACE_DIR

    if USE_DOCKER_SANDBOX:
        try:
            import docker
            client = docker.from_env()
            container = client.containers.run(
                DOCKER_IMAGE,
                command=f"sh -c {subprocess.list2cmdline([command])}",
                volumes={str(base): {"bind": "/workspace", "mode": "rw"}},
                working_dir="/workspace",
                detach=True,
                remove=False  # Do not auto-remove container before reading logs
            )
            # Wait for container execution to finish
            result = container.wait(timeout=timeout)
            logs = container.logs().decode("utf-8")
            container.remove(force=True)
            return logs[-8000:] if len(logs) > 8000 else logs
        except Exception as e:
            # Fallback to local subprocess if Docker unavailable
            pass

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=base,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        return output[-8000:] if len(output) > 8000 else output
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {command}"

def git(args: str, workspace_dir: Optional[Path] = None) -> str:
    return run_command(f"git {args}", workspace_dir=workspace_dir)

def git_diff(args: str = "", workspace_dir: Optional[Path] = None) -> str:
    """Read-only git diff helper."""
    return run_command(f"git diff {args}", workspace_dir=workspace_dir)

ALL_TOOL_SCHEMAS = {
    "list_files": {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories at a path relative to the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path, default '.'"}},
            },
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full text contents of a file relative to the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file relative to the workspace root with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "run_command": {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command (e.g. a test runner or linter) inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    "git": {
        "type": "function",
        "function": {
            "name": "git",
            "description": "Run a git command (e.g. 'status', 'diff', 'add -A', 'commit -m \"msg\"') inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"args": {"type": "string"}},
                "required": ["args"],
            },
        },
    },
    "git_diff": {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Read-only operation to view git diffs inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"args": {"type": "string"}},
            },
        },
    },
}

TOOL_IMPLS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_command": run_command,
    "git": git,
    "git_diff": git_diff,
}

def get_tool_schemas_for_agent(allowed_tools: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if allowed_tools is None:
        return list(ALL_TOOL_SCHEMAS.values())
    return [schema for name, schema in ALL_TOOL_SCHEMAS.items() if name in allowed_tools]
