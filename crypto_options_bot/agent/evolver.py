"""Evolver — periodic self-improvement.

Every ``interval_sec`` (default 6 h) the Evolver:
    1. Pulls the recent trade journal from ``memory/history/``.
    2. Asks the LLM to identify patterns and propose parameter
       tweaks within the bounds declared in ``config/settings.yaml``.
    3. Low-risk proposals (single-parameter changes within a small
       delta) are deployed automatically.
    4. Higher-risk proposals are written to ``memory/proposals/``
       for human review (Telegram / GitHub PR).

The LLM CANNOT change strategy code, add new strategies, or remove
existing ones without human review. It can only nudge numeric
parameters that already exist in ``settings.yaml``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .llm import LLMClient
from .memory import Memory
from .tools import ToolCategory, ToolRegistry

log = logging.getLogger(__name__)

# Hard limits: how much any single proposal can move a parameter.
# Beyond these, the proposal is escalated to a human.
MAX_RELATIVE_CHANGE = 0.20  # ±20%
MAX_ABSOLUTE_CHANGE_BY_KEY: dict[str, float] = {
    "profit_target_pct": 5.0,        # pct points
    "wing_width": 50.0,              # absolute strikes
    "delta_threshold": 0.02,
    "max_open_positions": 1,
}


@dataclass
class EvolverProposal:
    proposal_id: str
    summary: str
    rationale: str
    diff: dict[str, Any]
    risk: str = "low"
    autodeploy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Evolver:
    project_root: Path
    memory: Memory
    llm: LLMClient
    settings_path: Optional[Path] = None
    tools: Optional[ToolRegistry] = None
    model: str = "minimax/MiniMax-M3"
    max_tokens: int = 1024
    interval_sec: float = 6 * 3600  # every 6 hours
    history_lookback: int = 200
    last_run_at: float = 0.0
    last_proposals: list[EvolverProposal] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).resolve()
        self.settings_path = (
            self.settings_path
            or self.project_root / "config" / "settings.yaml"
        )
        self.review_prompt_path = (
            self.project_root / "crypto_options_bot" / "agent" / "prompts" / "evolver_review.md"
        )

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def run_once(self) -> list[EvolverProposal]:
        """Review the recent journal and produce 0..N proposals."""
        self.last_run_at = time.time()
        self.memory.append_journal("evolver", "starting evolver review cycle")

        trade_history = self.memory.read_history("trader_decisions", limit=self.history_lookback)
        health_history = self.memory.read_health_today()
        # Pull live P&L from paper_state if available.
        paper_state = self._read_paper_state()

        proposals = self._ask_llm(trade_history, health_history, paper_state)
        if not proposals:
            self.memory.append_journal("evolver", "no actionable patterns detected")
            return []

        applied: list[EvolverProposal] = []
        for p in proposals:
            try:
                self._validate(p)
                self._classify_risk(p)
                self._maybe_enable_autodeploy(p)
            except ValueError as exc:
                # Bad proposal — reject it but don't kill the whole cycle.
                self.memory.update_proposal_status(p.proposal_id, "rejected", note=str(exc))
                self.memory.append_journal(
                    "evolver",
                    f"proposal={p.proposal_id} rejected: {exc}",
                )
                continue
            self.memory.write_proposal(
                proposal_id=p.proposal_id,
                summary=p.summary,
                rationale=p.rationale,
                diff=p.diff,
                risk=p.risk,
                autodeploy=p.autodeploy,
            )
            self.memory.append_journal(
                "evolver",
                f"proposal={p.proposal_id} risk={p.risk} autodeploy={p.autodeploy} "
                f"summary={p.summary[:160]}",
            )
            if p.autodeploy:
                self._apply(p)
                self.memory.update_proposal_status(p.proposal_id, "deployed")
            self.last_proposals.append(p)

        # Trim history.
        if len(self.last_proposals) > 50:
            del self.last_proposals[: len(self.last_proposals) - 50]
        return proposals

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------
    def _ask_llm(
        self,
        trade_history: list[dict[str, Any]],
        health_history: list[dict[str, Any]],
        paper_state: dict[str, Any],
    ) -> list[EvolverProposal]:
        try:
            review_template = self._load_prompt(self.review_prompt_path)
        except FileNotFoundError:
            log.warning("evolver: review prompt missing; skipping cycle")
            return []

        # Pull current settings snapshot for grounding.
        current = self._load_settings_snapshot()
        ctx = {
            "current_settings": current,
            "trade_decisions_recent": trade_history[-30:],
            "health_today": health_history[-20:],
            "paper_state_summary": paper_state,
        }
        ctx_json = json.dumps(ctx, default=str, indent=2)[:30_000]
        user_prompt = review_template.replace("{{CONTEXT}}", ctx_json)

        try:
            resp = self.llm.messages(
                model=self.model,
                system=(
                    "You are the Evolver for a crypto options paper-trading bot. "
                    "Review recent decisions and propose small, safe parameter tweaks. "
                    "Respond with strict JSON only."
                ),
                messages=[{"role": "user", "content": user_prompt}],
                max_tokens=self.max_tokens,
                temperature=0.3,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("evolver: LLM call failed (%s); skipping", exc)
            return []

        parsed = self._parse_response(resp.text)
        if not parsed:
            return []
        out: list[EvolverProposal] = []
        for raw in parsed.get("proposals", []):
            p = self._coerce(raw)
            if p is not None:
                out.append(p)
        return out

    @staticmethod
    def _parse_response(text: str) -> Optional[dict[str, Any]]:
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _coerce(raw: dict[str, Any]) -> Optional[EvolverProposal]:
        try:
            pid = (
                raw.get("id")
                or f"evo-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{abs(hash(raw.get('summary',''))) % 10_000}"
            )
            diff = raw.get("diff") or {}
            if not isinstance(diff, dict) or not diff:
                return None
            return EvolverProposal(
                proposal_id=str(pid),
                summary=str(raw.get("summary", ""))[:200],
                rationale=str(raw.get("rationale", ""))[:500],
                diff=diff,
            )
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Safety: validate + classify
    # ------------------------------------------------------------------
    def _validate(self, proposal: EvolverProposal) -> None:
        """Raise if the diff violates safety rails."""
        for key, new_value in proposal.diff.items():
            old_value = self._current_value(key)
            if old_value is None:
                raise ValueError(f"unknown settings key: {key!r}")
            # Reject non-numeric changes.
            try:
                new_f = float(new_value)
                old_f = float(old_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"non-numeric value for {key!r}: {new_value}") from exc
            if old_f == 0:
                continue
            rel = abs(new_f - old_f) / max(abs(old_f), 1e-9)
            abs_limit = MAX_ABSOLUTE_CHANGE_BY_KEY.get(key)
            too_big_rel = rel > MAX_RELATIVE_CHANGE
            too_big_abs = abs_limit is not None and abs(new_f - old_f) > abs_limit
            if too_big_rel or too_big_abs:
                proposal.risk = "high"
                proposal.autodeploy = False

    def _classify_risk(self, proposal: EvolverProposal) -> None:
        """Risk = low by default; 'high' if multiple keys or risky changes."""
        if len(proposal.diff) >= 3:
            proposal.risk = "high"
            proposal.autodeploy = False
            return
        # Anything touching max_open_positions (last key segment) escalates.
        for k in proposal.diff:
            last = k.rsplit(".", 1)[-1]
            if last == "max_open_positions" or "max_open_positions" in k:
                proposal.risk = "high"
                proposal.autodeploy = False
                return

    def _maybe_enable_autodeploy(self, proposal: EvolverProposal) -> None:
        """Low-risk proposals are deployed immediately; high-risk stay as proposals."""
        if proposal.risk == "low":
            proposal.autodeploy = True

    # ------------------------------------------------------------------
    # Apply (low-risk only)
    # ------------------------------------------------------------------
    def _apply(self, proposal: EvolverProposal) -> None:
        if not self.settings_path or not self.settings_path.exists():
            log.warning("evolver: cannot apply; settings.yaml missing")
            return
        import re

        text = self.settings_path.read_text(encoding="utf-8")
        for k, new_value in proposal.diff.items():
            old_value = self._current_value(k)
            leaf = k.rsplit(".", 1)[-1]
            # Match the LEAF key at any indentation, but only as the key
            # of a YAML mapping line (followed by ':'). This avoids
            # accidental partial matches.
            pattern = re.compile(
                r"(?P<indent>^[ \t]*)(?P<key>" + re.escape(leaf) + r")(?P<sep>\s*:\s*)(?P<val>.*)$",
                re.MULTILINE,
            )
            replaced = False
            for m in pattern.finditer(text):
                # Skip lines that look like YAML list items or block
                # scalars; we only want scalar key: value.
                if m.group("val").strip().startswith("-"):
                    continue
                # Skip block scalars with multi-line values.
                line = text[m.start(): text.find("\n", m.start()) if text.find("\n", m.start()) > 0 else len(text)]
                if line.rstrip().endswith("|") or line.rstrip().endswith(">"):
                    continue
                repl = (
                    f"{m.group('indent')}{m.group('key')}{m.group('sep')}{new_value}"
                )
                text = text[: m.start()] + repl + text[m.end():]
                replaced = True
                break
            if not replaced:
                log.warning(
                    "evolver: could not find key %r in settings.yaml; skipping",
                    k,
                )
                continue
            log.info(
                "evolver: applied %s: %s -> %s",
                k,
                old_value,
                new_value,
            )
        self.settings_path.write_text(text, encoding="utf-8")

    def _current_value(self, dotted_key: str) -> Any:
        snap = self._load_settings_snapshot()
        # Walk dotted.
        node: Any = snap
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def _load_settings_snapshot(self) -> dict[str, Any]:
        if not self.settings_path or not self.settings_path.exists():
            return {}
        try:
            import yaml  # type: ignore
            with open(self.settings_path, encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _read_paper_state(self) -> dict[str, Any]:
        path = self.project_root / "data_cache" / "paper_state.json"
        if not path.exists():
            return {}
        try:
            import json

            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _load_prompt(path: Path) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
