"""Tests for the Trader's wiring into the bot's _process_strategy loop.

These tests verify that:
- The Trader class is invoked after the risk engine and before execute_plan.
- VETO blocks execution.
- DOWNSIZE shrinks target_qty (never widens).
- APPROVE/HOLD-as-approve fall through to execution at the risk-approved qty.
- A missing/broken Trader gracefully falls through to risk-approved (no crash).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crypto_options_bot.agent.memory import Memory
from crypto_options_bot.agent.trader import TradeAction, Trader, TraderDecision


class _StubRiskDecision:
    def __init__(self, allowed: bool, qty: int = 1, preset: str = "base", reason: str = "ok"):
        self.allowed = allowed
        self.suggested_qty = qty
        self.preset = preset
        self.reason = reason


class _StubPlan:
    def __init__(self):
        self.legs = [
            {"side": "sell", "opt_type": "C", "strike": 4500, "delta": 0.20, "iv": 0.65},
            {"side": "sell", "opt_type": "P", "strike": 4000, "delta": 0.20, "iv": 0.65},
        ]
        self.target = 0.0520
        self.stop = 0.4160
        self.reason = "short_strangle: range + high IV"
        self.expiry = "2026-09-18"


class _StubStrategy:
    class _N:
        value = "short_strangle"
    name = _N()
    def is_eligible(self, ctx):
        return True
    def build_plan(self, ctx, account_state=None):
        return _StubPlan()


class _StubContext:
    underlying = "ETH"
    spot = 4250.0
    dvol = 51.0
    iv_rank = 50.0
    regime = "range_bound"
    timestamp = None  # filled at call time
    _momentum = 0.0
    _data_quality_bad = False


class _StubBroker:
    starting_capital = 100000.0
    _realized_pnl = 0.0
    def get_positions(self):
        return []


class _StubOrderMgr:
    def __init__(self):
        self.execute_calls: list = []
    def open_trades(self):
        return []
    def execute_plan(self, plan, qty, expiry):
        self.execute_calls.append({"plan": plan, "qty": qty, "expiry": expiry})


class _StubRisk:
    def check_trade(self, plan, account_state):
        return _StubRiskDecision(allowed=True, qty=1, preset="base")


class _StubFeed:
    def get_nearest_expiry(self, u):
        return None


def _build_runner_with_trader(trader):
    """Build a PaperRunner-like object whose only method under test is
    _process_strategy, so we don't have to spin up the whole bot."""
    from crypto_options_bot.__main__ import PaperRunner
    runner = PaperRunner(cfg={}, feed_mode="ws", mode="paper", trader=trader)
    runner.signal_log = mock.MagicMock()
    runner._last_plan_at = {}
    return runner


class TestTraderWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = Memory(root=Path(self.tmp.name) / "memory")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_trader(self, action: TradeAction, target_qty: int = 1) -> Trader:
        def _mock_llm(*_a, **_k):
            return mock.MagicMock(text='{"action":"' + action.value + '","target_qty":' + str(target_qty) + ',"rationale":"test"}')

        trader = Trader(
            project_root=Path(self.tmp.name),
            memory=self.mem,
            llm=mock.MagicMock(messages=_mock_llm, provider_status=lambda: [], budget_snapshot=lambda: {}),
            fallback_enabled=True,
        )
        # Bypass file-load of the prompt templates (the temp dir doesn't
        # contain the real prompts). The Trader will use the mocked LLM
        # response directly.
        trader._load_prompt = lambda *_a, **_k: "fake-system-prompt"
        return trader

    def test_veto_blocks_execution(self) -> None:
        trader = self._make_trader(TradeAction.VETO)
        runner = _build_runner_with_trader(trader)

        ctx = _StubContext()
        order_mgr = _StubOrderMgr()
        runner._process_strategy(
            _StubStrategy(), ctx,
            _StubBroker(), _StubFeed(),
            order_mgr, _StubRisk(),
        )

        # OrderManager.execute_plan should never be called.
        self.assertEqual(order_mgr.execute_calls, [],
                         "Trader VETO must NOT result in execute_plan being called")
        veto_calls = [
            call for call in runner.signal_log.append.call_args_list
            if call.kwargs.get("status") == "vetoed"
        ]
        self.assertGreater(len(veto_calls), 0,
                           "Trader VETO did not produce a 'vetoed' signal_log entry")

    def test_approve_calls_execute_plan(self) -> None:
        trader = self._make_trader(TradeAction.APPROVE, target_qty=1)
        runner = _build_runner_with_trader(trader)
        ctx = _StubContext()
        order_mgr = _StubOrderMgr()
        runner._process_strategy(
            _StubStrategy(), ctx,
            _StubBroker(), _StubFeed(),
            order_mgr, _StubRisk(),
        )
        self.assertEqual(len(order_mgr.execute_calls), 1,
                         "Trader APPROVE should result in exactly one execute_plan call")
        self.assertEqual(order_mgr.execute_calls[0]["qty"], 1)

    def test_approve_falls_through(self) -> None:
        trader = self._make_trader(TradeAction.APPROVE, target_qty=1)
        runner = _build_runner_with_trader(trader)
        ctx = _StubContext()
        runner._process_strategy(
            _StubStrategy(), ctx,
            _StubBroker(), _StubFeed(),
            _StubOrderMgr(), _StubRisk(),
        )
        approve_calls = [
            call for call in runner.signal_log.append.call_args_list
            if call.kwargs.get("status") == "approved"
        ]
        self.assertGreater(len(approve_calls), 0, "Trader APPROVE did not produce an 'approved' signal_log entry")

    def test_downsize_shrinks_qty(self) -> None:
        # Risk allows qty=2; Trader says DOWNSIZE to qty=1
        class _StubRiskDownsize(_StubRisk):
            def check_trade(self, plan, account_state):
                return _StubRiskDecision(allowed=True, qty=2, preset="base")

        trader = self._make_trader(TradeAction.DOWNSIZE, target_qty=1)
        runner = _build_runner_with_trader(trader)
        ctx = _StubContext()
        runner._process_strategy(
            _StubStrategy(), ctx,
            _StubBroker(), _StubFeed(),
            _StubOrderMgr(), _StubRiskDownsize(),
        )
        # The signal_log should record a "downsized" entry.
        downsize_calls = [
            call for call in runner.signal_log.append.call_args_list
            if call.kwargs.get("status") == "downsized"
        ]
        self.assertGreater(len(downsize_calls), 0, "Trader DOWNSIZE did not produce a 'downsized' signal_log entry")

    def test_downsize_cannot_widen_above_risk(self) -> None:
        """Trader DOWNSIZE to 5 — but risk only allowed 1. Should clamp at 1."""
        trader = self._make_trader(TradeAction.DOWNSIZE, target_qty=5)
        runner = _build_runner_with_trader(trader)
        ctx = _StubContext()
        runner._process_strategy(
            _StubStrategy(), ctx,
            _StubBroker(), _StubFeed(),
            _StubOrderMgr(), _StubRisk(),  # qty=1
        )
        # With target_qty clamped to 1, this should be APPROVED at qty=1
        # (because new_qty (1) is NOT < decision.suggested_qty (1)).
        approved_or_downsized = [
            call for call in runner.signal_log.append.call_args_list
            if call.kwargs.get("status") in ("approved", "downsized")
        ]
        self.assertGreater(len(approved_or_downsized), 0)
        # And no veto either — DOWNSIZE to same as approved qty is APPROVE.
        veto_calls = [
            call for call in runner.signal_log.append.call_args_list
            if call.kwargs.get("status") == "vetoed"
        ]
        self.assertEqual(veto_calls, [])

    def test_trader_crash_falls_through_to_risk_approved(self) -> None:
        """If Trader.decide_cycle raises, the bot must still execute the
        risk-approved plan. Trader is enhancement, not blocker."""

        class _BrokenTrader:
            name = "broken"
            def decide_cycle(self, **_kw):
                raise RuntimeError("LLM timeout (simulated)")

        runner = _build_runner_with_trader(_BrokenTrader())
        ctx = _StubContext()
        runner._process_strategy(
            _StubStrategy(), ctx,
            _StubBroker(), _StubFeed(),
            _StubOrderMgr(), _StubRisk(),
        )
        approved_or_downsized = [
            call for call in runner.signal_log.append.call_args_list
            if call.kwargs.get("status") in ("approved", "downsized")
        ]
        self.assertGreater(len(approved_or_downsized), 0,
                           "Trader crash should fall through to risk-approved execution")


if __name__ == "__main__":
    unittest.main()
