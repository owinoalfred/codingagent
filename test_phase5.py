import sys
import os
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agent-api"))

import db
from fastapi.testclient import TestClient
from app import app

def main():
    print("--- Running Phase 5 Verification ---")

    # Insert sample task and usage records
    task_id = db.create_task("Observability Dashboard Test", "Verify metrics")
    db.log_model_usage(task_id, "run-dash", "qwen3-coder-next", 1000, 500, 0.002)

    client = TestClient(app)
    response = client.get("/v1/dashboard")

    print(f"1. Dashboard HTTP Status: {response.status_code}")
    assert response.status_code == 200
    assert "Personal AI Engineer — Observability Dashboard" in response.text
    assert "Observability Dashboard Test" in response.text
    assert "Total Tokens" in response.text
    assert "Total Cost (USD)" in response.text

    print("--- Phase 5 Verification PASSED ---")

if __name__ == "__main__":
    main()
