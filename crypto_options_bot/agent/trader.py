"""Trader — LLM-driven decision wrapper.

Wraps the existing ``crypto_options_bot`` strategy / execution layer.
Adds an LLM veto + sizing layer on top: every cycle, the bot's
strategies propose candidate ``TradePlan`` objects; the Trader asks
the LLM to either ``APPROVE``, ``VETO``, or ``DOWNSIZE`` each one.

Hard rails (never overridable by the LLM)
-----------------------------------------
* The risk engine (``crypto_options_bot.risk.engine.RiskEngine``) is
  the final gate. Every plan must pass it before execution.
* Daily loss cap, max positions, per-trade stop are enforced here
  *in addition* to whatever the LLM says.
* The Trader NEVER directly calls the live ``DeribitClient``. Live
  orders must go through ``OrderManager.execute_plan``.

The LLM's job is to add discretionary intelligence:
* "Today is FOMC, skip new opens"
* "DVOL dropped 10 pts, shrink iron condor size"
* "Realised loss in last 5 trades > 2% — sit out the rest of the day"

If the LLM is unavailable or budget is exhausted, the Trader
falls back to rule-based execution (``LLM_FALLBACK = True``).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .llm import LLMClient, LLMResponse
from .memory import Memory

log = logging.getLogger(__name__)


class TradeAction(str, Enum):
    APPROVE = "approve"
    VETO = "veto"
    DOWNSIZE = "downsize"
    HOLD = "hold"


@dataclass
class TraderDecision:
    action: TradeAction
    rationale: str
    target_qty: int = 1
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "rationale": self.rationale,
            "target_qty": self.target_qty,
            "raw": self.raw,
        }


@dataclass
class Trader:
    """LLM-driven decision layer.

    Construct once at process start, then call ``decide_cycle()``
    every N seconds (typically 5) with a fresh ``signal_context``
    snapshot.
    """

    project_root: Path
    memory: Memory
    llm: LLMClient
    fallback_enabled: bool = True
    daily_loss_breached: bool = False
    last_decision: Optional[TraderDecision] = None
    decided_cycles: int = 0
    vetoed_cycles: int = 0
    approved_cycles: int = 0
    fallback_cycles: int = 0
    model: str = "minimax/MiniMax-M3"
    max_tokens: int = 256

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).resolve()
        self.system_prompt_path = (
            self.project_root / "crypto_options_bot" / "agent" / "prompts" / "trader_system.md"
        )
        self.decision_prompt_path = (
            self.project_root / "crypto_options_bot" / "agent" / "prompts" / "trader_decision.md"
        )

    # ------------------------------------------------------------------
    # Main decision entry
    # ------------------------------------------------------------------
    def decide_cycle(
        self,
        *,
        signal_context: dict[str, Any],
        candidate_plans: list[dict[str, Any]],
        account_state: dict[str, Any],
        health_summary: dict[str, Any],
    ) -> TraderDecision:
        """Decide what to do this cycle.

        Args:
            signal_context: Spot/dvol/iv_rank snapshot from DeribitFeed.
            candidate_plans: TradePlans proposed by strategies this cycle
                              (may be empty).
            account_state: cash / total / positions / P&L snapshot.
            health_summary: from Sentinel.

        Returns:
            TraderDecision (APPROVE/VETO/DOWNSIZE/HOLD).
        """
        self.decided_cycles += 1

        # 1. Local hard kill switches.
        if self.daily_loss_breached:
            return self._record(
                TraderDecision(
                    TradeAction.VETO,
                    "daily-loss-cap reached — refusing to trade",
                ),
                used_fallback=False,
            )

        if not candidate_plans:
            return self._record(
                TraderDecision(TradeAction.HOLD, "no candidate plans this cycle"),
                used_fallback=False,
            )

        if health_summary.get("bot_alive") is False:
            return self._record(
                TraderDecision(TradeAction.VETO, "bot_alive=False; deferring to Healer"),
                used_fallback=False,
            )

        # 2. Ask the LLM.
        decision = self._ask_llm(signal_context, candidate_plans, account_state, health_summary)
        if decision is None:
            # 3. Fallback: rule-based approve.
            self.fallback_cycles += 1
            decision = TraderDecision(
                TradeAction.APPROVE,
                "LLM unavailable or budget exceeded; rule-based fallback APPROVE",
                target_qty=1,
            )
        return self._record(decision, used_fallback=False)

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------
    def _ask_llm(
        self,
        signal_context: dict[str, Any],
        candidate_plans: list[dict[str, Any]],
        account_state: dict[str, Any],
        health_summary: dict[str, Any],
    ) -> Optional[TraderDecision]:
        try:
            system_prompt = self._load_prompt(self.system_prompt_path)
            decision_template = self._load_prompt(self.decision_prompt_path)
        except FileNotFoundError as exc:
            log.warning("trader: prompt missing, falling back: %s", exc)
            return None

        # Render the user prompt.
        ctx_json = json.dumps(
            {
                "signal_context": signal_context,
                "candidate_plans": candidate_plans,
                "account_state": account_state,
                "health_summary": health_summary,
            },
            default=str,
            indent=2,
        )[:20_000]
        user_prompt = decision_template.replace("{{CONTEXT}}", ctx_json)

        try:
            resp: LLMResponse = self.llm.messages(
                model=self.model,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                max_tokens=self.max_tokens,
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("trader: LLM call failed (%s); falling back", exc)
            return None

        parsed = self._parse_response(resp.text)
        if parsed is None:
            return None
        action_str = (parsed.get("action") or "").lower()
        if action_str not in {a.value for a in TradeAction}:
            log.warning("trader: invalid LLM action %r", action_str)
            return None
        return TraderDecision(
            action=TradeAction(action_str),
            rationale=str(parsed.get("rationale", ""))[:500],
            target_qty=int(parsed.get("target_qty", 1)),
            raw=parsed,
        )

    @staticmethod
    def _parse_response(text: str) -> Optional[dict[str, Any]]:
        # Prefer a JSON block, fall back to loose extraction.
        text = text.strip()
        # Strip ``` fences if present.
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Loose: look for an action line.
        for ln in text.splitlines():
            low = ln.lower()
            if "action" in low and ":" in ln:
                _, _, rhs = ln.partition(":")
                rhs = rhs.strip().strip("`").strip().lower()
                if rhs in {a.value for a in TradeAction}:
                    return {"action": rhs, "rationale": text[:400], "target_qty": 1}
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _record(self, decision: TraderDecision, used_fallback: bool) -> TraderDecision:
        self.last_decision = decision
        if decision.action == TradeAction.VETO:
            self.vetoed_cycles += 1
        elif decision.action == TradeAction.APPROVE:
            self.approved_cycles += 1
        self.memory.append_history(
            "trader_decisions",
            {
                "ts": time.time(),
                "action": decision.action.value,
                "rationale": decision.rationale[:200],
                "target_qty": decision.target_qty,
            },
        )
        if used_fallback:
            self.fallback_cycles += 1
        return decision

    @staticmethod
    def _load_prompt(path: Path) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
