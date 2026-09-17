"""Agent layer for crypto-options-bot.

A 6-agent self-evolving, self-healing, self-realizing system that runs
24/7 on top of the existing paper-trading bot:

    Sentinel  - is everything alive?
    Healer    - fix what's broken (no strategy changes)
    Trader    - LLM-driven trade decisions within hard risk rails
    Evolver   - analyse journal, propose parameter / strategy changes
    Reflector - daily review, write lessons/ to memory
    Operator  - the always-on loop that wakes the others

Public entry points live in ``crypto_options_bot.__main__`` (the
``operator`` subcommand) and in ``crypto_options_bot.agent.operator``.
"""
from __future__ import annotations

from .llm import LLMClient, LLMError, LLMResponse  # noqa: F401
from .memory import Memory  # noqa: F401
from .scheduler import Scheduler  # noqa: F401
from .tools import ToolRegistry  # noqa: F401

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "Memory",
    "Scheduler",
    "ToolRegistry",
]
