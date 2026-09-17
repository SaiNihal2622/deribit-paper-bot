"""Live demo of the Trader agent — uses the actual Trader class + actual
bot prompts, not a mock. Builds a realistic Deribit scenario, runs
the Trader through its full pipeline, prints the decision, and saves
it to the memory journal so it shows up in `memory/journal/`.

Run anytime:
    python scripts/trader_live_demo.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # type: ignore
load_dotenv(ROOT / ".env", override=False)

from crypto_options_bot.agent.llm import LLMClient  # noqa: E402
from crypto_options_bot.agent.memory import Memory  # noqa: E402
from crypto_options_bot.agent.trader import Trader  # noqa: E402


def build_scenario(
    underlying: str = "ETH",
    spot: float = 4250.0,
    iv: float = 0.65,
    iv_rank: float = 50.0,
    dvol: float = 51.0,
    open_positions: int = 4,
    max_positions: int = 4,
    preset: str = "base",
    plan_reason: str = "short_strangle: range + high IV (iv_rank=50), credit=0.1038",
) -> dict:
    return {
        "signal_context": {
            "underlying": underlying,
            "spot": spot,
            "dvol": dvol,
            "iv_rank": iv_rank,
            "regime": "range_bound",
            "momentum": 0.005,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "candidate_plans": [{
            "strategy": "short_strangle",
            "underlying": underlying,
            "reason": plan_reason,
            "target": 0.0520,
            "stop": 0.4160,
            "legs": [
                {"side": "sell", "opt_type": "C", "strike": int(spot * 1.05), "delta": 0.20, "iv": iv},
                {"side": "sell", "opt_type": "P", "strike": int(spot * 0.95), "delta": 0.20, "iv": iv},
            ],
            "risk_qty": 1,
            "preset": preset,
        }],
        "account_state": {
            "capital": 100000.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": -0.0058,
            "open_positions": open_positions,
            "max_positions": max_positions,
            "momentum": 0.005,
        },
        "health_summary": {"bot_alive": True, "ws_subscribed": 22},
    }


def main() -> int:
    print("=" * 64)
    print("  Trader agent — live decision via the actual Trader class")
    print("=" * 64)

    # Use the real prompts + real LLMClient + real Memory.
    settings_path = ROOT / "config" / "settings.yaml"
    llm = LLMClient(settings_path=settings_path)
    mem = Memory(root=ROOT / "memory")
    trader = Trader(
        project_root=ROOT,
        memory=mem,
        llm=llm,
        fallback_enabled=True,
    )
    print(f"Trader model: {trader.model}")
    print(f"Providers loaded: {len(llm.provider_status())}")
    for s in llm.provider_status():
        if s["key_present"]:
            print(f"  - {s['name']} (key={s['key_present']})")
    print()

    # Scenario A: position cap hit (what's actually happening on the bot)
    print("-" * 64)
    print("  Scenario A: open=4 max=4 (position cap hit — likely VETO)")
    print("-" * 64)
    s_a = build_scenario(open_positions=4, max_positions=4)
    d_a = trader.decide_cycle(**s_a)
    print(f"  action     : {d_a.action.value}")
    print(f"  target_qty : {d_a.target_qty}")
    print(f"  rationale  : {d_a.rationale}")
    print()

    # Scenario B: room for one more (likely APPROVE)
    print("-" * 64)
    print("  Scenario B: open=2 max=4 (room for one more — likely APPROVE)")
    print("-" * 64)
    s_b = build_scenario(open_positions=2, max_positions=4)
    d_b = trader.decide_cycle(**s_b)
    print(f"  action     : {d_b.action.value}")
    print(f"  target_qty : {d_b.target_qty}")
    print(f"  rationale  : {d_b.rationale}")
    print()

    # Scenario C: tight stop, drawdown concern (likely VETO or DOWNSIZE)
    print("-" * 64)
    print("  Scenario C: daily_pnl=-1500 (losing day — likely DOWNSIZE)")
    print("-" * 64)
    s_c = build_scenario(open_positions=2, max_positions=4)
    s_c["account_state"]["realized_pnl"] = -1500.0
    s_c["candidate_plans"][0]["reason"] = (
        "short_strangle: range + high IV but daily loss at -1.5%, "
        "consider waiting until tomorrow's reset"
    )
    d_c = trader.decide_cycle(**s_c)
    print(f"  action     : {d_c.action.value}")
    print(f"  target_qty : {d_c.target_qty}")
    print(f"  rationale  : {d_c.rationale}")
    print()

    # Persist for visibility in journal + history
    mem.append_journal(
        "trader_live_demo",
        "Three live Trader decisions captured: "
        f"A={d_a.action.value}, B={d_b.action.value}, C={d_c.action.value}. "
        f"Budget used: {json.dumps(llm.budget_snapshot(), default=str)}",
    )

    print("=" * 64)
    print(f"  Budget: {json.dumps(llm.budget_snapshot(), default=str)}")
    print(f"  Decisions saved to memory/journal/{datetime.now().strftime('%Y-%m-%d')}.md")
    print(f"  History saved to memory/history/trader_decisions_{datetime.now().strftime('%Y-%m-%d')}.jsonl")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
