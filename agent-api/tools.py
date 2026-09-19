"""
Minimal tool set for the Phase 1 agent: filesystem, terminal, git.

Deliberately small on purpose — this is the MVP from the plan
("VS Code -> API -> model -> filesystem/terminal/git -> response").
Add permission checks / sandboxing (Docker, path allowlists) before
pointing this at anything you're not fine with the model editing directly.
"""
import subprocess
from pathlib import Path

# All file/terminal operations are restricted to this directory and below.
# Set via the WORKSPACE_DIR env var in .env — do not point this at your
# entire filesystem.
WORKSPACE_DIR = Path("./workspace").resolve()
WORKSPACE_DIR.mkdir(exist_ok=True)


def _safe_path(rel_path: str) -> Path:
    """Resolve a path and refuse to leave the workspace directory."""
    target = (WORKSPACE_DIR / rel_path).resolve()
    if WORKSPACE_DIR not in target.parents and target != WORKSPACE_DIR:
        raise ValueError(f"Path '{rel_path}' escapes the workspace directory.")
    return target


def list_files(path: str = ".") -> str:
    target = _safe_path(path)
    if not target.exists():
        return f"Path does not exist: {path}"
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return "\n".join(entries) if entries else "(empty directory)"


def read_file(path: str) -> str:
    target = _safe_path(path)
    if not target.exists():
        return f"File does not exist: {path}"
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"File is not text-decodable: {path}"


def write_file(path: str, content: str) -> str:
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} bytes to {path}"


def run_command(command: str, timeout: int = 60) -> str:
    """Run a shell command inside the workspace directory. No sudo, no
    network commands blocked here yet -- add an allowlist before you trust
    this with anything beyond your own throwaway repos."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        return output[-8000:] if len(output) > 8000 else output
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {command}"


def git(args: str) -> str:
    return run_command(f"git {args}")


TOOL_SCHEMAS = [
    {
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
    {
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
    {
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
    {
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
    {
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
]

TOOL_IMPLS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_command": run_command,
    "git": git,
}
