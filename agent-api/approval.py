"""
Tool risk classification and approval gates.
Classifies tool calls into SAFE, MODERATE, and DANGEROUS tiers.
"""
from typing import Dict, Any, Tuple

# Risk Tiers
SAFE = "SAFE"
MODERATE = "MODERATE"
DANGEROUS = "DANGEROUS"

DANGEROUS_COMMAND_PATTERNS = [
    "rm -rf",
    "git reset --hard",
    "git push --force",
    "git push -f",
    "dd ",
    "mkfs",
    "> /dev/",
    "chmod -R 777"
]

def classify_tool_risk(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """
    Returns SAFE, MODERATE, or DANGEROUS based on tool and arguments.
    """
    if tool_name in ["list_files", "read_file"]:
        return SAFE

    if tool_name == "write_file":
        return MODERATE

    if tool_name == "git":
        args = str(tool_args.get("args", "")).lower()
        if "push --force" in args or "push -f" in args or "reset --hard" in args or "clean -fd" in args:
            return DANGEROUS
        return MODERATE

    if tool_name == "run_command":
        cmd = str(tool_args.get("command", "")).lower()
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if pattern in cmd:
                return DANGEROUS
        return MODERATE

    # Default fallback for unknown tools
    return MODERATE
