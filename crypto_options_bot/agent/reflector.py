"""Reflector — daily review agent.

Runs once a day (default 00:05 IST). Reads:
    * the day's trades and trader decisions (``memory/history/trader_decisions``)
    * healer actions (``memory/health/<date>.jsonl``)
    * evolver proposals (``memory/proposals/``)
    * the live ``paper_state.json``

Asks the LLM to produce a Markdown report with sections:
    * What went well
    * What went badly
    * Surprises / anomalies
    * Tomorrow's plan
    * Concrete lessons to remember

Writes the result to ``memory/lessons/YYYY-MM-DD-<slug>.md``. The
Reflector is read-only outside its own memory area.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .llm import LLMClient
from .memory import Memory

log = logging.getLogger(__name__)


@dataclass
class Reflector:
    project_root: Path
    memory: Memory
    llm: LLMClient
    model: str = "minimax/MiniMax-M3"
    max_tokens: int = 2048
    last_lesson_path: Optional[Path] = None
    last_summary: str = ""

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).resolve()
        self.prompt_path = (
            self.project_root / "crypto_options_bot" / "agent" / "prompts" / "reflector_daily.md"
        )

    # ------------------------------------------------------------------
    # Main entry — call once per day
    # ------------------------------------------------------------------
    def run_once(self) -> Optional[Path]:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Gather context.
        trader_history = self.memory.read_history("trader_decisions", limit=200)
        health_today = self.memory.read_health_today()
        proposals_pending = self.memory.list_proposals(status="pending")
        proposals_deployed = self.memory.list_proposals(status="deployed")[-10:]
        paper_state = self._read_paper_state()
        prior_lessons = [p.name for p in self.memory.list_lessons(limit=14)]

        if not trader_history and not health_today:
            log.info("reflector: nothing to reflect on today; skipping")
            return None

        # 2. Ask the LLM.
        try:
            prompt = self._load_prompt(self.prompt_path)
        except FileNotFoundError:
            log.warning("reflector: prompt missing; skipping")
            return None

        ctx = {
            "date_utc": date,
            "prior_lessons": prior_lessons,
            "trader_decisions_count": len(trader_history),
            "trader_decisions_latest": trader_history[-15:],
            "health_today_count": len(health_today),
            "health_today_latest": health_today[-10:],
            "proposals_pending": proposals_pending,
            "proposals_deployed_recent": proposals_deployed,
            "paper_state": paper_state,
        }
        ctx_json = json.dumps(ctx, default=str, indent=2)[:30_000]
        user_prompt = prompt.replace("{{CONTEXT}}", ctx_json)

        try:
            resp = self.llm.messages(
                model=self.model,
                system=(
                    "You are the Reflector for a crypto options paper-trading bot. "
                    "Produce a daily Markdown reflection with these sections: "
                    "## What went well, ## What went badly, ## Surprises, "
                    "## Tomorrow's plan, ## Concrete lessons. Be honest, brief, "
                    "and specific. Respond in Markdown only, no preamble."
                ),
                messages=[{"role": "user", "content": user_prompt}],
                max_tokens=self.max_tokens,
                temperature=0.4,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("reflector: LLM call failed (%s); skipping", exc)
            return None

        body = resp.text.strip()
        self.last_summary = body[:500]

        # 3. Persist as a lesson.
        title = self._extract_title(body) or f"Daily reflection {date}"
        path = self.memory.write_lesson(title=title, body=body, tags=["reflector", "daily"])
        self.memory.append_journal(
            "reflector",
            f"wrote lesson: {path.name} bytes={path.stat().st_size}",
        )
        self.last_lesson_path = path
        return path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_title(body: str) -> Optional[str]:
        for ln in body.splitlines():
            ln = ln.strip()
            if ln.startswith("#"):
                return ln.lstrip("#").strip()[:80]
        return None

    def _read_paper_state(self) -> dict[str, Any]:
        path = self.project_root / "data_cache" / "paper_state.json"
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _load_prompt(path: Path) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
