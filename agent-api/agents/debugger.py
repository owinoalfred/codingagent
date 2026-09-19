"""
Debugger Agent: Analyzes test failures, locates bugs, and applies patches.
"""
from typing import List, Dict, Any

ROLE = "debugger"

SYSTEM_PROMPT = """You are the Debugger Agent. Your job is to analyze failed test outputs, locate the root cause in the codebase, and patch the bug.
Read the test failure log carefully, inspect the offending source files, write targeted fixes, and confirm the fix."""

ALLOWED_TOOLS = ["list_files", "read_file", "write_file", "run_command", "git"]
