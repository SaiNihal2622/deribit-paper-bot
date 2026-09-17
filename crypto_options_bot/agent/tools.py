"""Tool registry for LLM-callable functions.

Each tool is a single function registered with a name, JSON-schema-ish
description, and a category (read / write-state / write-config).
The Trader / Evolver agents expose these to their LLM as function-call
options. ``ToolRegistry.call()`` dispatches by name and journals every
invocation.

Hard rules enforced here:

    * READ tools never mutate state.
    * WRITE-STATE tools touch ``memory/state`` and ``memory/journal``
      only — never the live broker.
    * WRITE-CONFIG tools mutate ``config/settings.yaml`` (Evolver
      proposals). They require an explicit ``proposal_id`` parameter;
      the operator checks this id against ``memory/proposals`` before
      allowing the call.
    * No tool ever sends an exchange order directly. The Trader agent
      must call the existing ``OrderManager.execute_plan()`` through
      the read path, with the risk engine as a hard rail.

This separation is what keeps the LLM from accidentally placing a
live Deribit order.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .memory import Memory


class ToolCategory(str, Enum):
    READ = "read"
    WRITE_STATE = "write_state"
    WRITE_CONFIG = "write_config"


@dataclass
class ToolDef:
    name: str
    description: str
    category: ToolCategory
    fn: Callable[..., Any]
    parameters_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    result: Any
    duration_sec: float
    error: str = ""


class ToolRegistry:
    """In-process registry of LLM-callable functions."""

    def __init__(self, memory: Optional[Memory] = None) -> None:
        self._tools: dict[str, ToolDef] = {}
        self._recent: list[ToolCall] = []
        self._memory = memory

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(
        self,
        name: str,
        description: str,
        category: ToolCategory,
        parameters_schema: Optional[dict[str, Any]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._tools:
                raise ValueError(f"tool already registered: {name!r}")
            self._tools[name] = ToolDef(
                name=name,
                description=description,
                category=category,
                fn=fn,
                parameters_schema=parameters_schema or {},
            )
            return fn

        return decorator

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def call(self, name: str, args: Optional[dict[str, Any]] = None) -> Any:
        args = dict(args or {})
        tool = self._tools.get(name)
        if tool is None:
            return self._record(name, args, None, 0.0, error=f"unknown tool: {name!r}")
        t0 = time.time()
        try:
            result = tool.fn(**args)
        except Exception as exc:  # noqa: BLE001
            return self._record(name, args, None, time.time() - t0, error=str(exc))
        return self._record(name, args, result, time.time() - t0)

    def _record(
        self,
        name: str,
        args: dict[str, Any],
        result: Any,
        duration_sec: float,
        error: str = "",
    ) -> dict[str, Any]:
        call = ToolCall(tool=name, args=args, result=result, duration_sec=duration_sec, error=error)
        self._recent.append(call)
        if len(self._recent) > 500:
            del self._recent[: len(self._recent) - 500]
        if self._memory is not None:
            self._memory.append_journal(
                "tools",
                f"tool={name} args={json.dumps(args, default=str)[:200]} "
                f"ok={'yes' if not error else 'no'} dur={duration_sec:.3f}s "
                f"err={error}",
            )
        out = {"ok": not error, "result": result, "error": error, "duration_sec": duration_sec}
        return out

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "category": t.category.value,
                "parameters": t.parameters_schema,
            }
            for t in self._tools.values()
        ]

    def recent_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "tool": c.tool,
                "args": c.args,
                "ok": not c.error,
                "error": c.error,
                "duration_sec": round(c.duration_sec, 4),
            }
            for c in self._recent[-limit:]
        ]
