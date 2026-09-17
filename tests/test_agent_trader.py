"""Tests for the Trader agent."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crypto_options_bot.agent.llm import LLMClient, LLMResponse
from crypto_options_bot.agent.memory import Memory
from crypto_options_bot.agent.trader import TradeAction, Trader


def _llm(reply: str) -> LLMClient:
    def _mock(*_args, **_kwargs):
        return LLMResponse(text=reply, model="mock", input_tokens=1, output_tokens=1)

    return LLMClient(api_key="sk-test", mock=_mock)


def _write_prompts(project_root: Path) -> None:
    pd = project_root / "crypto_options_bot" / "agent" / "prompts"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "trader_system.md").write_text("You are a trader.", encoding="utf-8")
    (pd / "trader_decision.md").write_text("CTX={{CONTEXT}}", encoding="utf-8")


class TestTrader(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mem = Memory(root=self.root / "memory")
        _write_prompts(self.root)
        self.trader = Trader(
            project_root=self.root,
            memory=self.mem,
            llm=_llm('{"action":"approve","target_qty":1,"rationale":"fine"}'),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_candidates_returns_hold(self) -> None:
        d = self.trader.decide_cycle(
            signal_context={"spot": 100},
            candidate_plans=[],
            account_state={"cash": 100_000},
            health_summary={"bot_alive": True},
        )
        self.assertEqual(d.action, TradeAction.HOLD)

    def test_llm_approve(self) -> None:
        d = self.trader.decide_cycle(
            signal_context={"spot": 100},
            candidate_plans=[{"plan_id": "p1", "strategy": "iron_condor", "qty": 1}],
            account_state={"cash": 100_000},
            health_summary={"bot_alive": True},
        )
        self.assertEqual(d.action, TradeAction.APPROVE)
        self.assertEqual(d.target_qty, 1)
        self.assertEqual(self.trader.approved_cycles, 1)

    def test_llm_veto(self) -> None:
        self.trader.llm = _llm('{"action":"veto","target_qty":1,"rationale":"too risky"}')
        d = self.trader.decide_cycle(
            signal_context={"spot": 100},
            candidate_plans=[{"plan_id": "p1", "strategy": "iron_condor", "qty": 1}],
            account_state={"cash": 100_000},
            health_summary={"bot_alive": True},
        )
        self.assertEqual(d.action, TradeAction.VETO)
        self.assertEqual(self.trader.vetoed_cycles, 1)

    def test_daily_loss_killswitch(self) -> None:
        self.trader.daily_loss_breached = True
        d = self.trader.decide_cycle(
            signal_context={"spot": 100},
            candidate_plans=[{"plan_id": "p1", "strategy": "iron_condor", "qty": 1}],
            account_state={"cash": 100_000},
            health_summary={"bot_alive": True},
        )
        self.assertEqual(d.action, TradeAction.VETO)
        self.assertIn("daily-loss", d.rationale)

    def test_bot_dead_health_veto(self) -> None:
        d = self.trader.decide_cycle(
            signal_context={"spot": 100},
            candidate_plans=[{"plan_id": "p1", "strategy": "iron_condor", "qty": 1}],
            account_state={"cash": 100_000},
            health_summary={"bot_alive": False},
        )
        self.assertEqual(d.action, TradeAction.VETO)

    def test_llm_fallback_on_invalid_json(self) -> None:
        self.trader.llm = _llm("not json, just prose")
        d = self.trader.decide_cycle(
            signal_context={"spot": 100},
            candidate_plans=[{"plan_id": "p1", "strategy": "iron_condor", "qty": 1}],
            account_state={"cash": 100_000},
            health_summary={"bot_alive": True},
        )
        # Falls back to APPROVE per default rule.
        self.assertEqual(d.action, TradeAction.APPROVE)
        self.assertEqual(self.trader.fallback_cycles, 1)

    def test_llm_fallback_on_error(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("network down")

        self.trader.llm = LLMClient(api_key="sk-test", mock=boom)
        d = self.trader.decide_cycle(
            signal_context={"spot": 100},
            candidate_plans=[{"plan_id": "p1", "strategy": "iron_condor", "qty": 1}],
            account_state={"cash": 100_000},
            health_summary={"bot_alive": True},
        )
        self.assertEqual(d.action, TradeAction.APPROVE)

    def test_loose_action_parsing(self) -> None:
        self.trader.llm = _llm("action: downsize\nrationale: half size")
        d = self.trader.decide_cycle(
            signal_context={"spot": 100},
            candidate_plans=[{"plan_id": "p1", "strategy": "iron_condor", "qty": 2}],
            account_state={"cash": 100_000},
            health_summary={"bot_alive": True},
        )
        self.assertEqual(d.action, TradeAction.DOWNSIZE)

    def test_decisions_journaled(self) -> None:
        self.trader.decide_cycle(
            signal_context={"spot": 100},
            candidate_plans=[{"plan_id": "p1", "strategy": "iron_condor", "qty": 1}],
            account_state={"cash": 100_000},
            health_summary={"bot_alive": True},
        )
        # Decision recorded in history.
        out = self.mem.read_history("trader_decisions")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "approve")


if __name__ == "__main__":
    unittest.main()
