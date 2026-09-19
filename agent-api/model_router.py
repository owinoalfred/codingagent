"""
Model router abstraction.
Routes LLM calls to standard or cheap model providers based on task complexity/type.
"""
import os
from abc import ABC, abstractmethod
from typing import Tuple, List, Dict, Any

class ModelProvider(ABC):
    @abstractmethod
    def get_endpoint_and_model(self) -> Tuple[str, str]:
        pass

class QwenSelfHostedProvider(ModelProvider):
    def __init__(self):
        self.endpoint = os.getenv("MODEL_ENDPOINT", "http://localhost:8000/v1")
        self.model_name = os.getenv("MODEL_NAME", "qwen3-coder-next")

    def get_endpoint_and_model(self) -> Tuple[str, str]:
        return self.endpoint, self.model_name

class CheapModelProvider(ModelProvider):
    def __init__(self):
        self.endpoint = os.getenv("CHEAP_MODEL_ENDPOINT", os.getenv("MODEL_ENDPOINT", "http://localhost:8000/v1"))
        self.model_name = os.getenv("CHEAP_MODEL_NAME", "qwen2.5-coder-7b")

    def get_endpoint_and_model(self) -> Tuple[str, str]:
        return self.endpoint, self.model_name

def is_low_complexity_task(messages: List[Dict[str, Any]]) -> bool:
    """
    Evaluates if task/prompt is low complexity (e.g., docstrings, typos, formatting).
    """
    for msg in messages:
        content = str(msg.get("content", "")).lower()
        if any(keyword in content for keyword in ["docstring", "typo", "format", "rename", "comment"]):
            return True
    return False

def get_model_provider(task_id: str, messages: List[Dict[str, Any]]) -> ModelProvider:
    if is_low_complexity_task(messages):
        return CheapModelProvider()
    return QwenSelfHostedProvider()
