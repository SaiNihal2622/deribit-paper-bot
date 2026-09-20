"""Operator — the always-on top-level loop.

Owns the agent registry, scheduler, and shared memory. Wires:

    Sentinel   every 60 s
    Healer     every 60 s, immediately after Sentinel
    Trader     every 5 s (tick-aligned with the bot's own cycle)
    Evolver    every 6 hours
    Reflector  once per day at 00:05 UTC
    Heartbeat  every 30 s — writes its own liveness ping so external
               watchdogs can detect a stuck Operator

Two operational modes:

    ``run_forever()`` — daemon mode. Wires SIGINT/SIGTERM for graceful
    shutdown. Used by ``scripts/start_operator.ps1``.

    ``run_until(stop_event)`` — test-friendly mode. Used by integration
    tests.

If the Operator itself crashes, the bot keeps running independently
because the Trader / Healer also runs as standalone cron jobs. The
Operator is an *enhancement*, not a single point of failure.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .evolver import Evolver
from .healer import Healer
from .llm import LLMClient, LLMError
from .memory import Memory
from .reflector import Reflector
from .scheduler import Scheduler
from .sentinel import Sentinel
from .tools import ToolCategory, ToolRegistry
from .trader import Trader

log = logging.getLogger(__name__)

# Wire up the agent layer's stdlib logger to write to stderr (which NSSM
# redirects to logs/operator_stderr.log). Without a handler, log.info() calls
# are silently dropped because Python's logging defaults to WARNING-level
# output with no handler attached. We use the agent logger name as the
# prefix so the NSSM log file clearly identifies agent-layer output.
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-5s | %(name)s:%(funcName)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    # Don't double-log via root logger if anyone adds a root handler later.
    log.propagate = False


@dataclass
class OperatorConfig:
    project_root: Path
    memory_dir: Path = Path("memory")
    sentinel_interval_sec: float = 60.0
    evolver_interval_sec: float = 6 * 3600.0
    reflector_at_hhmm: str = "00:05"
    heartbeat_interval_sec: float = 30.0
    heartbeat_path: Optional[Path] = None
    llm_model: str = "minimax/MiniMax-M3"
    enable_trader: bool = True
    enable_evolver: bool = True
    enable_reflector: bool = True
    llm_probe_interval_sec: float = 3600.0  # 1 hour


@dataclass
class Operator:
    config: OperatorConfig
    memory: Memory = field(init=False)
    llm: LLMClient = field(init=False)
    tools: ToolRegistry = field(init=False)
    sentinel: Sentinel = field(init=False)
    healer: Healer = field(init=False)
    trader: Trader = field(init=False)
    evolver: Evolver = field(init=False)
    reflector: Reflector = field(init=False)
    scheduler: Scheduler = field(default_factory=lambda: Scheduler(name="operator"))
    started_at: float = 0.0
    cycles: int = 0
    _last_llm_probe_at: float = 0.0

    def __post_init__(self) -> None:
        self.config.project_root = Path(self.config.project_root).resolve()
        if not self.config.heartbeat_path:
            self.config.heartbeat_path = (
                self.config.project_root / "data_cache" / "operator.heartbeat"
            )

        # Subsystems.
        self.memory = Memory(root=self.config.memory_dir)
        settings_path = self.config.project_root / "config" / "settings.yaml"
        self.llm = LLMClient(settings_path=settings_path)
        self.tools = ToolRegistry(memory=self.memory)
        self._register_tools()

        self.sentinel = Sentinel(
            project_root=self.config.project_root,
            memory=self.memory,
        )
        self.healer = Healer(
            project_root=self.config.project_root,
            memory=self.memory,
            sentinel=self.sentinel,
        )
        self.trader = Trader(
            project_root=self.config.project_root,
            memory=self.memory,
            llm=self.llm,
        )
        self.evolver = Evolver(
            project_root=self.config.project_root,
            memory=self.memory,
            llm=self.llm,
            tools=self.tools,
        )
        self.reflector = Reflector(
            project_root=self.config.project_root,
            memory=self.memory,
            llm=self.llm,
        )

        # Schedule.
        self.scheduler.add(
            "sentinel",
            self._tick_sentinel,
            every_sec=self.config.sentinel_interval_sec,
        )
        self.scheduler.add(
            "healer",
            self._tick_healer,
            every_sec=self.config.sentinel_interval_sec,
        )
        self.scheduler.add(
            "evolver",
            self._tick_evolver,
            every_sec=self.config.evolver_interval_sec,
        )
        self.scheduler.add(
            "reflector",
            self._tick_reflector,
            at_hhmm=self.config.reflector_at_hhmm,
        )
        self.scheduler.add(
            "heartbeat",
            self._tick_heartbeat,
            every_sec=self.config.heartbeat_interval_sec,
        )
        self.scheduler.add(
            "llm_probe",
            self._tick_llm_probe,
            every_sec=self.config.llm_probe_interval_sec,
        )

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def run_until(self, stop_event: threading.Event) -> None:
        """Test-friendly run loop. Returns when stop_event is set."""
        self.started_at = time.time()
        log.info(
            "operator: starting in project_root=%s memory_dir=%s",
            self.config.project_root,
            self.config.memory_dir,
        )
        self.memory.append_journal("operator", "operator starting")
        self.scheduler.run_until(stop_event)
        log.info("operator: stopped after %.0fs", time.time() - self.started_at)
        self.memory.append_journal("operator", "operator stopped")

    def run_forever(self) -> None:
        """Daemon mode — install SIGINT/SIGTERM handlers, run forever."""
        stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, lambda *_: stop.set())
                signal.signal(signal.SIGTERM, lambda *_: stop.set())
            except ValueError:
                # Not in main thread (rare). Run without signal handlers.
                pass
        self.run_until(stop)

    def status(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "uptime_sec": time.time() - self.started_at if self.started_at else 0,
            "cycles": self.cycles,
            "scheduler": self.scheduler.list_jobs(),
            "trader": {
                "decided": self.trader.decided_cycles,
                "approved": self.trader.approved_cycles,
                "vetoed": self.trader.vetoed_cycles,
                "fallback": self.trader.fallback_cycles,
                "last_action": self.trader.last_decision.to_dict() if self.trader.last_decision else None,
            },
            "healer": {
                "recent": self.healer.recent_actions(5),
            },
            "evolver": {
                "last_run_at": self.evolver.last_run_at,
                "pending_proposals": len(self.memory.list_proposals("pending")),
                "recent_proposals": [p.to_dict() for p in self.evolver.last_proposals[-5:]],
            },
            "reflector": {
                "last_lesson": str(self.reflector.last_lesson_path) if self.reflector.last_lesson_path else None,
            },
            "llm_budget": self.llm.budget_snapshot(),
            "llm_providers": self.llm.provider_status(),
            "llm_last_probe_at": self._last_llm_probe_at,
            "tools_recent": self.tools.recent_calls(5),
        }

    # ------------------------------------------------------------------
    # Tick handlers
    # ------------------------------------------------------------------
    def _tick_sentinel(self) -> None:
        report = self.sentinel.probe()
        self.memory.write_state("sentinel:latest", report.to_dict())
        log.info("sentinel: %s", report.summary())

    def _tick_healer(self) -> None:
        report = self.sentinel.probe()
        actions = self.healer.heal(report)
        if actions:
            self.memory.write_state(
                "healer:latest",
                {"ts": time.time(), "actions": [a.to_dict() for a in actions]},
            )

    def _tick_evolver(self) -> None:
        if not self.config.enable_evolver:
            return
        self.evolver.run_once()

    def _tick_reflector(self) -> None:
        if not self.config.enable_reflector:
            return
        self.reflector.run_once()

    def _tick_heartbeat(self) -> None:
        self.cycles += 1
        payload = {
            "ts": time.time(),
            "cycles": self.cycles,
            "pid": os.getpid(),
            "scheduler": self.scheduler.list_jobs(),
        }
        path = self.config.heartbeat_path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)

    def _tick_llm_probe(self) -> None:
        """Hourly: try every configured provider with a one-token call.

        Surfaces which providers actually work, logs the result to the
        memory journal so the user can see what to fix in .env. The next
        Trader/ Evolver/ Reflector call picks up the freshly-validated
        provider automatically.
        """
        self._last_llm_probe_at = time.time()
        statuses = []
        working = []
        for st in self.llm.provider_status():
            statuses.append(st)
            if not st["key_present"] or st["dead_now"]:
                continue
            try:
                resp = self.llm.messages(
                    model=None,
                    system="Reply with the single word OK.",
                    messages=[{"role": "user", "content": "OK"}],
                    max_tokens=4,
                    provider=st["name"],
                )
                working.append(
                    {"provider": st["name"], "ok": True, "text": resp.text[:40]}
                )
            except Exception as exc:  # noqa: BLE001
                working.append(
                    {"provider": st["name"], "ok": False, "error": str(exc)[:200]}
                )
        self.memory.write_state(
            "llm_probe:latest",
            {"ts": time.time(), "providers": statuses, "results": working},
        )
        ok_count = sum(1 for r in working if r["ok"])
        log.info("llm_probe: %d/%d providers working", ok_count, len(working))
        if ok_count == 0:
            self.memory.append_journal(
                "operator",
                "LLM probe: NO provider authenticating. "
                "Edit .env to set a valid key (try MINIMAX_API_KEY, "
                "ANTHROPIC_API_KEY, OPENROUTER_API_KEY, GROQ_API_KEY, "
                "or MISTRAL_API_KEY). The next call will retry automatically.",
            )
        elif ok_count == 1:
            self.memory.append_journal(
                "operator",
                f"LLM probe: routed through '{working[0]['provider']}'",
            )

    # ------------------------------------------------------------------
    # Tools registered for the LLM layer
    # ------------------------------------------------------------------
    def _register_tools(self) -> None:
        mem = self.memory
        project = self.config.project_root

        @self.tools.register(
            name="read_paper_state",
            description="Read the bot's current paper trading state: cash, open positions, P&L, last error. No side effects.",
            category=ToolCategory.READ,
        )
        def read_paper_state() -> dict[str, Any]:
            path = project / "data_cache" / "paper_state.json"
            if not path.exists():
                return {}
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}

        @self.tools.register(
            name="read_settings",
            description="Read the active settings.yaml as JSON. No side effects.",
            category=ToolCategory.READ,
        )
        def read_settings() -> dict[str, Any]:
            path = project / "config" / "settings.yaml"
            if not path.exists():
                return {}
            try:
                import yaml  # type: ignore

                with open(path, encoding="utf-8") as fh:
                    return yaml.safe_load(fh) or {}
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}

        @self.tools.register(
            name="list_lessons",
            description="List recent lessons written by the Reflector.",
            category=ToolCategory.READ,
        )
        def list_lessons(limit: int = 10) -> list[str]:
            return [p.name for p in mem.list_lessons(limit=limit)]

        @self.tools.register(
            name="list_proposals",
            description="List evolver proposals (optionally filtered by status: pending|approved|deployed|rejected).",
            category=ToolCategory.READ,
        )
        def list_proposals(status: str | None = None) -> list[dict[str, Any]]:
            return mem.list_proposals(status=status)

        @self.tools.register(
            name="approve_proposal",
            description="Mark a pending proposal as approved-by-human. Doesn't apply the change.",
            category=ToolCategory.WRITE_STATE,
        )
        def approve_proposal(proposal_id: str, note: str = "") -> dict[str, Any]:
            mem.update_proposal_status(proposal_id, "approved", note=note or "human-approved")
            return {"ok": True, "proposal_id": proposal_id}

        @self.tools.register(
            name="reject_proposal",
            description="Mark a pending proposal as rejected-by-human.",
            category=ToolCategory.WRITE_STATE,
        )
        def reject_proposal(proposal_id: str, note: str = "") -> dict[str, Any]:
            mem.update_proposal_status(proposal_id, "rejected", note=note or "human-rejected")
            return {"ok": True, "proposal_id": proposal_id}

        @self.tools.register(
            name="write_journal",
            description="Append a free-form note to today's agent journal. Used by agents and humans.",
            category=ToolCategory.WRITE_STATE,
        )
        def write_journal(agent: str, text: str) -> dict[str, Any]:
            path = mem.append_journal(agent, text)
            return {"ok": True, "path": str(path)}
