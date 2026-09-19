"""
Database layer for agent state, tasks, messages, approvals, model usage, and decisions.
SQLite with thread-safe connection handling.
"""
import sqlite3
import json
import uuid
import os
from typing import Dict, Any, List, Optional
from pathlib import Path

DB_PATH = os.getenv("DATABASE_PATH", "agent_memory.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # Projects table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        root_path TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Tasks table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        parent_task_id TEXT,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL,
        worktree_path TEXT,
        branch_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (project_id) REFERENCES projects(id),
        FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
    );
    """)

    # Agent runs table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS agent_runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        agent_role TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        FOREIGN KEY (task_id) REFERENCES tasks(id)
    );
    """)

    # Messages table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        agent_run_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        tool_calls TEXT,
        tool_call_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id)
    );
    """)

    # Architectural decisions table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS architectural_decisions (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        title TEXT NOT NULL,
        rationale TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (project_id) REFERENCES projects(id)
    );
    """)

    # Model usage table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS model_usage (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        agent_run_id TEXT,
        model_name TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (task_id) REFERENCES tasks(id)
    );
    """)

    # Approvals table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        agent_run_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        tool_args TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        status TEXT NOT NULL, -- PENDING, APPROVED, REJECTED
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved_at TIMESTAMP,
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id)
    );
    """)

    conn.commit()
    conn.close()

# Helper Functions

def create_project(name: str, root_path: str) -> str:
    conn = get_db()
    project_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO projects (id, name, root_path) VALUES (?, ?, ?)",
        (project_id, name, root_path)
    )
    conn.commit()
    conn.close()
    return project_id

def create_task(title: str, description: str, project_id: Optional[str] = None, parent_task_id: Optional[str] = None) -> str:
    conn = get_db()
    task_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tasks (id, project_id, parent_task_id, title, description, status) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, project_id, parent_task_id, title, description, "PENDING")
    )
    conn.commit()
    conn.close()
    return task_id

def update_task_status(task_id: str, status: str, worktree_path: Optional[str] = None, branch_name: Optional[str] = None):
    conn = get_db()
    fields = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params = [status]
    if worktree_path is not None:
        fields.append("worktree_path = ?")
        params.append(worktree_path)
    if branch_name is not None:
        fields.append("branch_name = ?")
        params.append(branch_name)
    params.append(task_id)

    query = f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?"
    conn.execute(query, tuple(params))
    conn.commit()
    conn.close()

def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def create_agent_run(task_id: str, agent_role: str) -> str:
    conn = get_db()
    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO agent_runs (id, task_id, agent_role, status) VALUES (?, ?, ?, ?)",
        (run_id, task_id, agent_role, "RUNNING")
    )
    conn.commit()
    conn.close()
    return run_id

def update_agent_run_status(run_id: str, status: str):
    conn = get_db()
    conn.execute(
        "UPDATE agent_runs SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, run_id)
    )
    conn.commit()
    conn.close()

def log_message(agent_run_id: str, role: str, content: Optional[str], tool_calls: Optional[List[Dict]] = None, tool_call_id: Optional[str] = None):
    conn = get_db()
    msg_id = str(uuid.uuid4())
    tool_calls_json = json.dumps(tool_calls) if tool_calls else None
    conn.execute(
        "INSERT INTO messages (id, agent_run_id, role, content, tool_calls, tool_call_id) VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, agent_run_id, role, content, tool_calls_json, tool_call_id)
    )
    conn.commit()
    conn.close()

def log_model_usage(task_id: str, agent_run_id: Optional[str], model_name: str, prompt_tokens: int, completion_tokens: int, cost_usd: float):
    conn = get_db()
    usage_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO model_usage (id, task_id, agent_run_id, model_name, prompt_tokens, completion_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (usage_id, task_id, agent_run_id, model_name, prompt_tokens, completion_tokens, cost_usd)
    )
    conn.commit()
    conn.close()

def get_task_total_cost(task_id: str) -> float:
    conn = get_db()
    row = conn.execute("SELECT SUM(cost_usd) as total_cost FROM model_usage WHERE task_id = ?", (task_id,)).fetchone()
    conn.close()
    return row["total_cost"] if row and row["total_cost"] is not None else 0.0

def request_approval(task_id: str, agent_run_id: str, tool_name: str, tool_args: Dict[str, Any], risk_level: str) -> str:
    conn = get_db()
    approval_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO approvals (id, task_id, agent_run_id, tool_name, tool_args, risk_level, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (approval_id, task_id, agent_run_id, tool_name, json.dumps(tool_args), risk_level, "PENDING")
    )
    conn.commit()
    conn.close()
    return approval_id

def resolve_approval(approval_id: str, status: str, reason: Optional[str] = None):
    conn = get_db()
    conn.execute(
        "UPDATE approvals SET status = ?, reason = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, reason, approval_id)
    )
    conn.commit()
    conn.close()

def get_approval(approval_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

# Initialize on import
init_db()
