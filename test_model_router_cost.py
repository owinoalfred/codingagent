import sys
import os
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agent-api"))

import db
import model_router

def test_model_router_low_vs_high_complexity():
    low_messages = [{"role": "user", "content": "Please fix a typo in the README comments."}]
    provider = model_router.get_model_provider("task-low", low_messages)
    assert isinstance(provider, model_router.CheapModelProvider)
    _, model_name = provider.get_endpoint_and_model()
    assert model_name == "qwen2.5-coder-7b"

    high_messages = [{"role": "user", "content": "Implement a distributed task scheduler with retry policies."}]
    provider2 = model_router.get_model_provider("task-high", high_messages)
    assert isinstance(provider2, model_router.QwenSelfHostedProvider)
    _, model_name2 = provider2.get_endpoint_and_model()
    assert model_name2 == "qwen3-coder-next"

def test_cost_accounting_and_totals():
    task_id = db.create_task("Cost Acc Task", "Testing cost calculations")
    db.log_model_usage(task_id, "run-1", "qwen3-coder-next", 1000, 500, 0.002)
    db.log_model_usage(task_id, "run-2", "qwen2.5-coder-7b", 2000, 1000, 0.001)

    total_cost = db.get_task_total_cost(task_id)
    assert abs(total_cost - 0.003) < 0.00001
