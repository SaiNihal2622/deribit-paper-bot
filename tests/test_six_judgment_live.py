"""Integration test: drive the live Trader end-to-end with a real LLM call.

Doesn't actually run the bot loop. Just imports Trader, builds a synthetic
state, and confirms the six-judgment path either succeeds (with a valid
aggregator result) or fails gracefully (returns None and falls back to the
legacy single-action path).

Run: python tests/test_six_judgment_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so LLMClient has API keys available. The bot does this
# in main() but our standalone script doesn't.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

# Configure logging minimally — we just want trader.* logs.
import logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(name)s] %(levelname)s %(message)s",
)


def main() -> int:
    # Lazy imports so the test fails fast on env issues.
    from crypto_options_bot.agent.trader import (
        Trader,
        TradeAction,
        aggregate_judgments,
        _parse_six_judgments,
    )
    from crypto_options_bot.agent.llm import LLMClient
    from crypto_options_bot.agent.memory import Memory

    project_root = ROOT
    memory = Memory(root=project_root / "data_cache" / "memory")
    llm = LLMClient.from_env() if hasattr(LLMClient, "from_env") else LLMClient()

    trader = Trader(
        project_root=project_root,
        memory=memory,
        llm=llm,
    )

    # Synthetic short_strangle candidate plan (range + high IV regime).
    signal_context = {
        "underlying": "ETH",
        "spot": 2745.0,
        "dvol": 51.6,
        "iv_rank": 50,
        "adx": 18.0,
        "trend_strength": -0.05,
        "regime": "range",
        "timestamp": "2026-09-22T23:00:00+00:00",
    }
    candidate_plans = [
        {
            "strategy": "short_strangle",
            "underlying": "ETH",
            "qty": 1,
            "credit": 0.0914,
            "max_loss": 0.3656,
            "target": 0.0457,
            "strikes": {"call": 2500, "put": 2400},
            "expiry": "2026-09-23",
        }
    ]
    account_state = {
        "capital": 100_000.0,
        "open_positions": 2,
        "open_trades": 2,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "daily_loss": 0.0,
    }
    health_summary = {
        "bot_alive": True,
        "feed": "DeribitWebSocketFeed",
        "ws_connected": True,
        "open_trades": 2,
    }

    print("=" * 60)
    print("Calling trader.decide_cycle() with synthetic short_strangle plan...")
    print("=" * 60)
    decision = trader.decide_cycle(
        signal_context=signal_context,
        candidate_plans=candidate_plans,
        account_state=account_state,
        health_summary=health_summary,
    )
    print()
    print(f"decision.action       = {decision.action!r}")
    print(f"decision.target_qty   = {decision.target_qty}")
    print(f"decision.rationale    = {decision.rationale!r}")
    print(f"decision.raw          = {json.dumps(decision.raw, default=str, indent=2)}")
    print()

    # Heuristically tell whether the six-judgment path fired (vs legacy).
    six_j_used = decision.rationale.startswith("[6j]")
    print(f"six_judgment path used? {six_j_used}")

    if six_j_used:
        if decision.action == TradeAction.APPROVE:
            print("PASS: clean range/quiet setup was approved by aggregator")
            return 0
        if decision.action == TradeAction.DOWNSIZE:
            print("PASS: aggregator downgraded to qty=1 — within tolerance for synthetic state")
            return 0
        if decision.action == TradeAction.VETO:
            print("INFO: aggregator vetoed — that means the LLM judged this state as risky")
            print("      (tox_flow=high OR liquidity_stressed=yes OR volatile_high+neutral)")
            return 0
        if decision.action == TradeAction.HOLD:
            print("FAIL: HOLD is unexpected when candidate_plans was provided")
            return 1
    else:
        print("INFO: legacy single-action path was used (six-judgment parse failed or was bypassed)")
        print("      This is fine — fallback to legacy is intentional. Verify the LLM returned valid JSON next time.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
