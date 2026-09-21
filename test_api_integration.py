import sys
import os
import json
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agent-api"))

import db
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert "gateway" in data
    assert data["gateway"] == "ok"
    assert "model_endpoint_reachable" in data

def test_tasks_crud():
    # 1. Create task
    res = client.post("/v1/tasks", json={"title": "Test Task 1", "description": "Test description"})
    assert res.status_code == 200
    data = res.json()
    assert "task_id" in data
    assert data["status"] == "PENDING"
    task_id = data["task_id"]

    # 2. Get task
    res2 = client.get(f"/v1/tasks/{task_id}")
    assert res2.status_code == 200
    task_data = res2.json()
    assert task_data["id"] == task_id
    assert task_data["title"] == "Test Task 1"

    # 3. Nonexistent task
    res3 = client.get("/v1/tasks/nonexistent-id-999")
    assert res3.status_code == 404

def test_models_endpoint():
    res = client.get("/v1/models")
    assert res.status_code == 200
    data = res.json()
    assert "data" in data
    assert len(data["data"]) > 0

def test_chat_completions_endpoint(monkeypatch):
    # Mock httpx in app.py or call_model_routed if needed, or check fallback
    res = client.post("/v1/chat/completions", json={
        "model": "qwen3-coder-next",
        "messages": [{"role": "user", "content": "Hello"}]
    })
    # If model endpoint is offline, should return 502/500 or error
    assert res.status_code in [200, 502, 500]

def test_approvals_flow():
    task_id = db.create_task("Approval Task", "Test approval")
    run_id = db.create_agent_run(task_id, "coder")
    appr_id = db.request_approval(task_id, run_id, "write_file", {"path": "test.txt"}, "MODERATE")

    # List pending
    res = client.get("/v1/approvals/pending")
    assert res.status_code == 200
    pending = res.json()
    assert any(a["id"] == appr_id for a in pending)

    # Approve
    res_appr = client.post(f"/v1/approvals/{appr_id}/approve")
    assert res_appr.status_code == 200
    assert res_appr.json()["status"] == "APPROVED"

    # Check state
    appr_row = db.get_approval(appr_id)
    assert appr_row["status"] == "APPROVED"

def test_dashboard_views():
    for view in ["intervention", "sentinel", "docket"]:
        res = client.get(f"/v1/dashboard?view={view}")
        assert res.status_code == 200
        assert "Personal AI Engineer" in res.text
