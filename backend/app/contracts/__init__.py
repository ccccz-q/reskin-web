"""契约层 —— 工具 Schema 的唯一权威定义

对外暴露：TOOLS / openai_tools() / validate_call() / SPEND_TOOLS
"""
from contracts.tools import (          # noqa: F401
    SPEND_TOOLS,
    TOOL_NAMES,
    TOOLS,
    TOOL_SPEC_VERSION,
    openai_tools,
    validate_call,
)
