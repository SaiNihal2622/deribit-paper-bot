"""Tests for the IV-regime circuit breaker, DTE filter, and mainnet readiness helper."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# _helpers.dte_from_ddmmyy + filter_expiries_by_min_dte
# ---------------------------------------------------------------------------
from crypto_options_bot.strategy._helpers import (
    dte_from_ddmmyy,
    filter_expiries_by_min_dte,
)


class TestDteFromDdmmyy:
    def test_today_is_zero(self):
        today = datetime.now(timezone.utc).date()
        ddmmyy = today.strftime("%d%b%y").upper()
        assert dte_from_ddmmyy(ddmmyy) == 0

    def test_one_week_ahead(self):
        future = datetime.now(timezone.utc).date() + timedelta(days=7)
        ddmmyy = future.strftime("%d%b%y").upper()
        assert dte_from_ddmmyy(ddmmyy) == 7

    def test_one_month_ahead(self):
        future = datetime.now(timezone.utc).date() + timedelta(days=30)
        ddmmyy = future.strftime("%d%b%y").upper()
        assert dte_from_ddmmyy(ddmmyy) == 30

    def test_invalid_returns_none(self):
        assert dte_from_ddmmyy("") is None
        assert dte_from_ddmmyy("bogus!!") is None
        assert dte_from_ddmmyy(None) is None  # type: ignore[arg-type]
        # past dates still return a numeric (negative) DTE; the caller can decide
        # how to interpret them. We only guarantee None for unparseable input.
        assert dte_from_ddmmyy("99ZZZ99") is None


class TestFilterExpiriesByMinDte:
    def test_drops_zero_and_one_dte(self):
        today = datetime.now(timezone.utc).date()
        d0 = today.isoformat()
        d1 = (today + timedelta(days=1)).isoformat()
        d7 = (today + timedelta(days=7)).isoformat()
        d30 = (today + timedelta(days=30)).isoformat()
        out = filter_expiries_by_min_dte([d0, d1, d7, d30], min_dte=2)
        assert out == [d7, d30]

    def test_keeps_everything_when_min_dte_is_zero(self):
        today = datetime.now(timezone.utc).date()
        d0 = today.isoformat()
        d1 = (today + timedelta(days=1)).isoformat()
        out = filter_expiries_by_min_dte([d0, d1], min_dte=0)
        assert out == [d0, d1]

    def test_ignores_unparseable_dates(self):
        out = filter_expiries_by_min_dte(["bogus", "2026-13-99"], min_dte=2)
        assert out == []

    def test_drops_past_dates_implicitly(self):
        past = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()
        future = (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()
        out = filter_expiries_by_min_dte([past, future], min_dte=2)
        assert out == [future]


# ---------------------------------------------------------------------------
# short_call DTE filter
# ---------------------------------------------------------------------------
from crypto_options_bot.strategy.base import SignalContext
from crypto_options_bot.strategy.short_call import ShortCallStrategy


def _ctx(spot: float = 65_000.0, dvol: float = 50.0, iv_rank: float = 50.0,
         expiry_ddmmyy: str = "") -> SignalContext:
    strikes = [60_000.0, 62_000.0, 64_000.0, 65_000.0, 66_000.0, 68_000.0, 70_000.0]
    option_ltps = {
        (60_000.0, "C"): 5500.0,
        (62_000.0, "C"): 3500.0,
        (64_000.0, "C"): 1500.0,
        (65_000.0, "C"): 800.0,
        (66_000.0, "C"): 400.0,
        (68_000.0, "C"): 200.0,
        (70_000.0, "C"): 80.0,
    }
    return SignalContext(
        underlying="BTC",
        spot=spot,
        dvol=dvol,
        iv_rank=iv_rank,
        adx=10.0,
        trend_strength=0.0,
        regime="range",
        timestamp=datetime.now(timezone.utc),
        strikes=strikes,
        option_ltps=option_ltps,
        option_ivs={k: 0.65 for k in option_ltps},
        expiry_ddmmyy=expiry_ddmmyy,
    )


class TestShortCallDteFilter:
    def test_today_dte_rejected_when_min_dte_is_2(self):
        today_ddmmyy = datetime.now(timezone.utc).date().strftime("%d%b%y").upper()
        strat = ShortCallStrategy({"min_dte_to_trade": 2})
        plan = strat.build_plan(_ctx(expiry_ddmmyy=today_ddmmyy), account_state={})
        assert plan is None, "must reject 0DTE plans when min_dte=2"

    def test_one_dte_rejected_when_min_dte_is_2(self):
        future = datetime.now(timezone.utc).date() + timedelta(days=1)
        ddmmyy = future.strftime("%d%b%y").upper()
        strat = ShortCallStrategy({"min_dte_to_trade": 2})
        plan = strat.build_plan(_ctx(expiry_ddmmyy=ddmmyy), account_state={})
        assert plan is None, "must reject 1DTE plans when min_dte=2"

    def test_seven_dte_accepted(self):
        future = datetime.now(timezone.utc).date() + timedelta(days=7)
        ddmmyy = future.strftime("%d%b%y").upper()
        strat = ShortCallStrategy({"min_dte_to_trade": 2})
        plan = strat.build_plan(_ctx(expiry_ddmmyy=ddmmyy), account_state={})
        assert plan is not None, "7DTE must pass the min_dte=2 filter"

    def test_dte_filter_disabled_when_min_dte_is_zero(self):
        today_ddmmyy = datetime.now(timezone.utc).date().strftime("%d%b%y").upper()
        strat = ShortCallStrategy({"min_dte_to_trade": 0})
        plan = strat.build_plan(_ctx(expiry_ddmmyy=today_ddmmyy), account_state={})
        assert plan is not None, "0DTE must be accepted when min_dte=0"

    def test_no_expiry_ddmmyy_skips_dte_filter(self):
        # If the context has no expiry (multi-expiry mixes strikes), the
        # strategy should NOT refuse — we trust the upstream gating.
        strat = ShortCallStrategy({"min_dte_to_trade": 2})
        plan = strat.build_plan(_ctx(expiry_ddmmyy=""), account_state={})
        assert plan is not None


# ---------------------------------------------------------------------------
# IV-regime gate (in __main__.py)
# ---------------------------------------------------------------------------
# We don't import __main__ (too heavy) — we test the gate logic in isolation
# by constructing a minimal stub.
from dataclasses import dataclass


@dataclass
class _StubCtx:
    underlying: str
    dvol: float
    iv_rank: float


class TestRegimeGateLogic:
    """Tests the regime-gate logic without spinning up the whole bot."""

    def _gate(self, dvol: float, iv_rank: float, cur: str,
              enabled: bool = True,
              thresholds: dict = None) -> tuple[bool, str]:
        """Replicates the gate check from __main__._process_strategy."""
        if not enabled:
            return True, "gate disabled"
        thresholds = thresholds or {"BTC": (50.0, 40.0), "ETH": (55.0, 45.0)}
        min_dvol, min_iv_rank = thresholds.get(cur, (50.0, 40.0))
        reasons = []
        if dvol > 0 and dvol < min_dvol:
            reasons.append(f"dvol={dvol:.0f}<{min_dvol:.0f}")
        if iv_rank > 0 and iv_rank < min_iv_rank:
            reasons.append(f"iv_rank={iv_rank:.0f}<{min_iv_rank:.0f}")
        if reasons:
            return False, " / ".join(reasons)
        return True, "pass"

    def test_btc_low_dvol_blocks(self):
        ok, reason = self._gate(dvol=34.0, iv_rank=50.0, cur="BTC")
        assert not ok
        assert "dvol" in reason

    def test_btc_high_dvol_low_iv_rank_blocks(self):
        ok, reason = self._gate(dvol=60.0, iv_rank=30.0, cur="BTC")
        assert not ok
        assert "iv_rank" in reason

    def test_btc_high_dvol_high_iv_rank_passes(self):
        ok, _ = self._gate(dvol=60.0, iv_rank=70.0, cur="BTC")
        assert ok

    def test_eth_higher_threshold(self):
        # ETH min_dvol=55 — dvol=50 should block ETH but pass BTC
        ok_eth, _ = self._gate(dvol=50.0, iv_rank=50.0, cur="ETH")
        ok_btc, _ = self._gate(dvol=50.0, iv_rank=50.0, cur="BTC")
        assert not ok_eth
        assert ok_btc, "BTC min is 50 so 50 should pass"

    def test_disabled_gate_passes_everything(self):
        ok, _ = self._gate(dvol=10.0, iv_rank=10.0, cur="BTC", enabled=False)
        assert ok

    def test_zero_dvol_does_not_block(self):
        # DVOL=0 means we don't have data; let the strategy decide.
        ok, _ = self._gate(dvol=0.0, iv_rank=50.0, cur="BTC")
        assert ok

    def test_zero_iv_rank_does_not_block(self):
        ok, _ = self._gate(dvol=60.0, iv_rank=0.0, cur="BTC")
        assert ok


# ---------------------------------------------------------------------------
# mainnet_readiness.py
# ---------------------------------------------------------------------------
import importlib.util

spec = importlib.util.spec_from_file_location(
    "mainnet_readiness",
    ROOT / "scripts" / "mainnet_readiness.py",
)
assert spec and spec.loader, "could not load mainnet_readiness"
mnr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mnr)  # type: ignore[union-attr]


class TestMainnetReadiness:
    def test_collect_checks_returns_structured_list(self):
        checks = mnr.collect_checks()
        assert isinstance(checks, list)
        assert len(checks) >= 4
        for c in checks:
            assert "id" in c
            assert "status" in c
            assert c["status"] in ("PASS", "FAIL", "WARN", "INFO")
            assert "description" in c

    def test_detects_missing_env_vars(self, monkeypatch):
        # Clear any user env vars so we get a clean failure.
        for var in mnr.REQUIRED_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        checks = mnr.collect_checks()
        env_checks = [c for c in checks if c["category"] == "env"]
        assert all(c["status"] == "FAIL" for c in env_checks)
        for c in env_checks:
            assert c["required"] is True

    def test_renders_human_output(self, capsys):
        text = mnr.render_human(mnr.collect_checks())
        assert "MAINNET READINESS CHECK" in text
        assert "ACTION PLAN" in text
        assert "DERIBIT_CLIENT_ID" in text

    def test_json_output_is_valid(self, monkeypatch, capsys):
        for var in mnr.REQUIRED_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        # Invoke the main entry point with --json
        monkeypatch.setattr(sys, "argv", ["mainnet_readiness", "--json"])
        rc = mnr.main()
        assert rc == 1, f"expected exit code 1 with no env vars, got {rc}"
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert "checks" in payload
        assert isinstance(payload["checks"], list)
        # Required env-var checks must be flagged FAIL.
        env_checks = [c for c in payload["checks"] if c["category"] == "env"]
        assert all(c["status"] == "FAIL" for c in env_checks)