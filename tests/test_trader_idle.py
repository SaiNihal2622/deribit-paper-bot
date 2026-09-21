"""Tests for Trader.decide_idle() throttled HOLD-mode LLM check.

When no strategy produces a plan this cycle (e.g. regime gate closed
everything), the bot's main loop calls Trader.decide_idle() to ask the
LLM "do you agree with staying flat?". These tests verify the throttle
+ guard logic without needing real LLM access.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.agent.trader import TradeAction, Trader, TraderDecision  # noqa: E402


def _make_trader(**overrides) -> Trader:
    defaults = dict(
        project_root=ROOT,
        memory=MagicMock(),
        llm=MagicMock(),
        idle_check_interval_sec=1800.0,  # 30 min
    )
    defaults.update(overrides)
    return Trader(**defaults)


def _stub_decision(action=TradeAction.HOLD, rationale="vol too low"):
    return TraderDecision(action=action, rationale=rationale, target_qty=1)


def _ctx():
    return {
        "signal_context": {"underlying": "ALL", "dvol_btc": 36.0, "dvol_eth": 52.0},
        "account_state": {"cash": 100000.0, "realized_pnl": 0.0, "positions": 0},
        "health_summary": {"bot_alive": True, "preset": "base"},
    }


class TestIdleThrottle:
    def test_first_call_after_window_returns_decision(self):
        t = _make_trader()
        t.llm.messages.return_value = MagicMock(text='{"action":"hold","rationale":"vol low","target_qty":1}')
        ctx = _ctx()
        # Pin `now` to 100_000.0 — well past the throttle window from epoch 0.
        d = t.decide_idle(now=100_000.0, **ctx)
        assert d is not None
        assert d.action == TradeAction.HOLD
        assert t.idle_checks == 1

    def test_call_within_window_returns_none(self):
        t = _make_trader(idle_check_interval_sec=1800.0)
        t.llm.messages.return_value = MagicMock(text='{"action":"hold","rationale":"x","target_qty":1}')
        # First call at t=100_000 establishes last_idle_check_at
        first = t.decide_idle(now=100_000.0, **_ctx())
        assert first is not None
        # Second call 60s later is inside the 1800s window
        t.llm.messages.reset_mock()
        second = t.decide_idle(now=100_060.0, **_ctx())
        assert second is None
        assert not t.llm.messages.called
        assert t.idle_checks == 1

    def test_call_after_window_returns_new_decision(self):
        t = _make_trader(idle_check_interval_sec=1800.0)
        t.llm.messages.return_value = MagicMock(text='{"action":"hold","rationale":"x","target_qty":1}')
        first = t.decide_idle(now=100_000.0, **_ctx())
        assert first is not None
        # Now 2000s later — outside the window
        t.llm.messages.reset_mock()
        second = t.decide_idle(now=102_000.0, **_ctx())
        assert second is not None
        assert t.idle_checks == 2
        assert t.llm.messages.called


class TestIdleGuards:
    def test_daily_loss_breached_blocks_call(self):
        t = _make_trader(daily_loss_breached=True)
        ctx = _ctx()
        d = t.decide_idle(now=100_000.0, **ctx)
        assert d is None
        assert not t.llm.messages.called

    def test_bot_alive_false_blocks_call(self):
        t = _make_trader()
        ctx = _ctx()
        ctx["health_summary"]["bot_alive"] = False
        d = t.decide_idle(now=100_000.0, **ctx)
        assert d is None
        assert not t.llm.messages.called

    def test_zero_interval_first_call_still_fires(self):
        """Document: Trader with interval=0 fires once on the first call
        because `now - last >= 0` is trivially true. The bot's __main__
        guards the call site via `idle_check_interval_sec > 0` to disable
        idle checks entirely (the Trader itself doesn't enforce >= 1).
        """
        t = _make_trader(idle_check_interval_sec=0.0)
        t.llm.messages.return_value = MagicMock(text='{"action":"hold","rationale":"x","target_qty":1}')
        # Stub ctx without MagicMock values so json.dumps doesn't blow up
        ctx = {
            "signal_context": {"underlying": "ALL"},
            "account_state": {"cash": 1.0},
            "health_summary": {"bot_alive": True},
        }
        d = t.decide_idle(now=100_000.0, **ctx)
        assert d is not None
        assert t.idle_checks == 1


class TestIdleParseFallback:
    def test_garbled_llm_response_returns_none(self):
        """If LLM response doesn't parse, decide_idle returns None
        (rather than fabricating a decision)."""
        t = _make_trader()
        t.llm.messages.return_value = MagicMock(text="not json, no action line, useless")
        d = t.decide_idle(now=100_000.0, **_ctx())
        assert d is None
        # Still counted as an attempt
        assert t.idle_checks == 1

    def test_llm_exception_returns_none(self):
        t = _make_trader()
        t.llm.messages.side_effect = RuntimeError("provider down")
        d = t.decide_idle(now=100_000.0, **_ctx())
        assert d is None
        # Throttle still updated so we don't hammer a dead provider
        assert t.last_idle_check_at == 100_000.0
