"""引擎层 · Tool-use Loop —— 自建，替代 LangGraph

    engine/prompts.py   System Prompt 六层装配（前缀稳定，命中缓存）
    engine/loop.py      循环本体 + 三条护栏            ← 本模块
"""
from engine.loop import AgentRunResult, run  # noqa: F401

__all__ = ["AgentRunResult", "run"]
