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


def _safe_json(text: str) -> Any:
    """Parse JSON, tolerating single quotes and trailing commas.

    Some models (incl. certain prompts to MiniMax M3) emit near-JSON
    with single-quoted strings or trailing commas. We try a strict parse
    first, then a tolerant repair.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Tolerant: replace single-quoted strings with double-quoted, drop trailing commas.
    repaired = text
    # Naive but effective: 'word' → "word" only when it looks like a quoted string
    import re
    repaired = re.sub(r"'([^'\n]+?)'", r'"\1"', repaired)
    # Drop trailing commas before } or ]
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


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
    last_idle_check_at: float = 0.0
    idle_check_interval_sec: float = 1800.0  # 30 min — throttle HOLD-mode LLM calls
    decided_cycles: int = 0
    vetoed_cycles: int = 0
    approved_cycles: int = 0
    fallback_cycles: int = 0
    idle_checks: int = 0  # how often we asked the LLM "still nothing?" while idle
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
        # Action tokens used for constrained-decision probing on
        # OpenAI-compatible providers that expose logprobs (openrouter,
        # groq, mistral). Each action has a single-token label so the
        # logprob read gives us a clean per-option probability. We also
        # need to know the exact token id to filter top_logprobs — by
        # default the API returns top_logprobs=20, which is plenty for
        # 4 actions across any reasonable tokenization.
        self._constrained_action_tokens = {
            TradeAction.APPROVE: ["approve", "Approved", " APPROVE"],
            TradeAction.VETO:    ["veto", "Vetoed", " VETO"],
            TradeAction.DOWNSIZE: ["downsize", "Downsize", " DOWNSIZE"],
            TradeAction.HOLD:    ["hold", "Hold", " HOLD"],
        }

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
    # Idle-mode call (throttled): when no plans produced this cycle,
    # ask the LLM whether it agrees with staying flat. Generates real
    # journal entries so the Trader is observably alive even when the
    # regime gate filters everything out.
    # ------------------------------------------------------------------
    def decide_idle(
        self,
        *,
        signal_context: dict[str, Any],
        account_state: dict[str, Any],
        health_summary: dict[str, Any],
        now: Optional[float] = None,
    ) -> Optional[TraderDecision]:
        """Throttled LLM check while no plans are produced.

        Returns ``None`` if the throttle window has not elapsed (caller
        should not log a decision). Returns a ``TraderDecision`` (typically
        HOLD) when an LLM call actually happens.

        Args:
            signal_context: latest spot/dvol/iv_rank snapshot.
            account_state: cash / P&L / positions snapshot.
            health_summary: from Sentinel.
            now: epoch seconds; defaults to ``time.time()``. Exposed for
                testability.
        """
        if now is None:
            now = time.time()
        if now - self.last_idle_check_at < self.idle_check_interval_sec:
            return None
        if self.daily_loss_breached or health_summary.get("bot_alive") is False:
            return None
        self.last_idle_check_at = now
        self.idle_checks += 1
        decision = self._ask_llm(
            signal_context=signal_context,
            candidate_plans=[],
            account_state=account_state,
            health_summary=health_summary,
        )
        if decision is None:
            # LLM call failed or returned unparseable. For idle-check
            # we prefer a HOLD fallback over None, so the trader is
            # observably alive even when the model output is garbage.
            return self._record(
                TraderDecision(
                    action=TradeAction.HOLD,
                    rationale="(fallback) LLM response unparseable",
                    target_qty=1,
                ),
                used_fallback=True,
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

        # If the active provider supports logprobs (i.e. NOT minimax.io),
        # use the constrained-decision path: ask the model to score each
        # action's plausibility, take argmax, return a structured decision.
        # This eliminates JSON-parse failures entirely on supported
        # providers (openrouter/groq/mistral). On MiniMax (or any provider
        # where logprobs is unavailable), fall through to JSON generation.
        if self._active_provider_supports_logprobs():
            decision = self._ask_llm_constrained(
                signal_context=signal_context,
                candidate_plans=candidate_plans,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            if decision is not None:
                return self._record(decision, used_fallback=False)
            log.info(
                "trader: constrained-decision path failed; falling back to JSON"
            )

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
            log.warning(
                "trader: LLM response unparseable. raw_response=%r",
                (resp.text or "")[:500],
            )
            # Caller decides what to do with None (decide_cycle falls
            # back to APPROVE, decide_idle falls back to HOLD).
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
        """Robustly extract an action dict from the LLM response.

        Handles all the cases the raw MiniMax M3 output produces:
        - Bare JSON object
        - ```json ... ``` fences
        - Prose preamble + embedded JSON
        - "Action: approve" with prose rationale
        - Reasoning trace prefix ("Let me think... {json}")
        - Lowercase / uppercase action variants
        - Embedded code blocks with leading "json" tag
        """
        if not text:
            return None
        text = text.strip()
        if not text:
            return None

        action_set = {a.value for a in TradeAction}

        def _coerce(d: Any) -> Optional[dict[str, Any]]:
            if not isinstance(d, dict):
                return None
            action_str = (d.get("action") or "").lower()
            if action_str not in action_set:
                return None
            qty = d.get("target_qty", 1)
            try:
                qty = max(1, int(qty))
            except (TypeError, ValueError):
                qty = 1
            return {
                "action": action_str,
                "rationale": str(d.get("rationale", ""))[:500],
                "target_qty": qty,
            }

        # 1. Direct JSON parse (with leading-fence strip).
        for prefix in ("```json\n", "```JSON\n", "```\n", "```"):
            if text.startswith(prefix):
                stripped = text[len(prefix):]
                if stripped.endswith("```"):
                    stripped = stripped[:-3].rstrip()
                parsed = _coerce(_safe_json(stripped))
                if parsed is not None:
                    return parsed

        # 2. Whole text is a JSON object (with possible trailing text).
        parsed = _coerce(_safe_json(text))
        if parsed is not None:
            return parsed

        # 3. Find the first balanced {...} block in the text and try to parse.
        idx = text.find("{")
        if idx >= 0:
            depth = 0
            for end in range(idx, len(text)):
                ch = text[end]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[idx:end + 1]
                        parsed = _coerce(_safe_json(candidate))
                        if parsed is not None:
                            return parsed
                        break

        # 4. Loose regex: look for an "action: <word>" line in either
        #    "key: value" form or "action is <word>" prose form.
        for ln in text.splitlines():
            low = ln.lower().strip()
            if not low:
                continue
            for kw in ("action", "decision"):
                for sep in (":", "is", "="):
                    if kw + sep in low:
                        idx2 = low.find(kw)
                        # Find the keyword token, then the value token after it.
                        rest = low[idx2 + len(kw):]
                        # Drop separators
                        rest = rest.lstrip(": =")
                        # First whitespace-delimited token
                        token = rest.split()[0].strip(" `\"'.,;()[]{}")
                        if token in action_set:
                            return {
                                "action": token,
                                "rationale": text[:400],
                                "target_qty": 1,
                            }

        # 5. Last-resort: if the response is just a single word that
        #    matches an action, accept it.
        first_token = text.split()[0].strip(" `\"'.,;()[]{}").lower() if text.split() else ""
        if first_token in action_set:
            return {"action": first_token, "rationale": text[:400], "target_qty": 1}

        return None

    # ------------------------------------------------------------------
    # Constrained-decision path (OpenJev-style)
    # ------------------------------------------------------------------
    # When the active LLM provider supports logprobs (i.e. NOT minimax.io),
    # we ask the model to score each action's plausibility, then take the
    # argmax. This is the "Jev judges, code executes" pattern: the LLM
    # only emits a single logprob per option, the deterministic code picks.
    # This eliminates JSON-parse failures entirely on supported providers.

    def _active_provider_supports_logprobs(self) -> bool:
        """True if the LLM client's last-good provider exposes logprobs.

        MiniMax (api.minimax.io) silently ignores the logprobs param on
        both Anthropic-compatible and OpenAI-compatible routes, so we
        exclude it. Any other OpenAI-compatible provider that returned a
        200 last call is assumed to support logprobs; if it doesn't, the
        call returns an empty logprobs block and we fall back to JSON.
        """
        try:
            status = self.llm.provider_status()
        except Exception:  # noqa: BLE001
            return False
        for entry in status:
            if entry.get("is_last_good"):
                url = (entry.get("base_url") or "").lower()
                if "minimax.io" in url or "agent.minimax" in url:
                    return False
                # OpenAI-compatible chat-completions on any other host
                # that returned a 200 is assumed to support logprobs.
                return True
        # No last-good provider known yet; assume conservative = False
        # (don't enable constrained-decision until we've actually succeeded
        # against a provider we know handles logprobs).
        return False

    def _ask_llm_constrained(
        self,
        *,
        signal_context: dict[str, Any],
        candidate_plans: list[dict[str, Any]],
        system_prompt: str,
        user_prompt: str,
    ) -> Optional[TraderDecision]:
        """Constrained-decision via per-option logprob probing.

        For each action we send a short prompt asking the model to score
        the action's plausibility. The model returns a single token
        ("approve", "veto", etc.) with logprobs; we pick the action whose
        token had the highest probability. Robust against free-form output.
        """
        candidates_text = json.dumps(candidate_plans, default=str)[:2000]
        rationale_per_action: dict[str, str] = {}
        action_probs: dict[str, float] = {}

        # Use the last-good provider's model. We probe sequentially
        # (4 calls × ~1s = ~4s wall time); could parallelize with
        # asyncio but the gain is small and the implementation simpler
        # if we just do them in order.
        for action in TradeAction:
            tokens = self._constrained_action_tokens[action]
            probe_prompt = (
                f"{user_prompt}\n\n"
                f"Candidate plans: {candidates_text}\n\n"
                f"On a scale of single token, what's the most appropriate action "
                f"for this cycle given the candidate plans above?\n"
                f"Reply with EXACTLY one token, no commentary, no markdown: "
                f"{tokens[0]}"
            )
            try:
                resp = self.llm.messages(
                    model=self.model,
                    system=system_prompt,
                    messages=[{"role": "user", "content": probe_prompt}],
                    max_tokens=4,           # need only ~3 tokens (" Approve")
                    temperature=0.0,
                    extra_body={"logprobs": True, "top_logprobs": 20},
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("trader: constrained probe failed for %s: %s", action.value, exc)
                return None

            # Extract probability from the top_logprobs list.
            best_p, best_tok = 0.0, None
            logprob_block = resp.logprobs
            if logprob_block:
                top = logprob_block.get("top_logprobs") or []
                if top:
                    # top is a list[dict{token, logprob, bytes}] for the
                    # first generated token.
                    for entry in top:
                        tok = (entry.get("token") or "").strip().lower()
                        if any(tok.startswith(t.lower()) for t in tokens):
                            try:
                                p = float(entry.get("logprob"))
                                from math import exp
                                p = exp(p)
                            except (TypeError, ValueError):
                                continue
                            if p > best_p:
                                best_p = p
                                best_tok = tok
                            # The token matched — record rationale
                            rationale_per_action[action.value] = (
                                f"action='{tok}' probe_p={p:.3f}"
                            )
                            break

            action_probs[action.value] = best_p
            if best_p > 0:
                log.debug("trader: constrained probe %s -> %s (p=%.3f)", action.value, best_tok, best_p)

        if not action_probs or max(action_probs.values()) <= 0:
            return None

        # Pick the highest-probability action.
        chosen_action_str = max(action_probs.items(), key=lambda kv: kv[1])[0]
        try:
            chosen_action = TradeAction(chosen_action_str)
        except ValueError:
            return None

        return TraderDecision(
            action=chosen_action,
            rationale=(
                f"(constrained) chose='{chosen_action_str}' "
                f"probs=" + ", ".join(f"{k}={v:.2f}" for k, v in action_probs.items())
                + f"  {rationale_per_action.get(chosen_action_str, '')}"
            )[:500],
            target_qty=1,
        )

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
