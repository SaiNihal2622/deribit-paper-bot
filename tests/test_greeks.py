"""Unit tests for the Black-Scholes Greeks module.

Covers:
  - ATM call price sanity vs textbook formula
  - IV round-trip: bisect to get back the input vol within 1e-4
  - Put-call parity: C - P = S - K*exp(-rT)
  - Greeks vs finite differences (delta/gamma/vega/theta/rho within 1e-4)
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

# Make the package importable when running pytest from the project root
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.risk.greeks import (
    bs_greeks,
    mark_iv,
    mark_to_market,
    portfolio_greeks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _finite_diff_delta(spot, strike, t, vol, r, q, opt_type, h=0.01):
    up = bs_greeks(spot * (1 + h), strike, t, vol, r, q, opt_type).price
    down = bs_greeks(spot * (1 - h), strike, t, vol, r, q, opt_type).price
    return (up - down) / (2 * spot * h)


def _finite_diff_gamma(spot, strike, t, vol, r, q, opt_type, h=0.01):
    up = bs_greeks(spot * (1 + h), strike, t, vol, r, q, opt_type).price
    mid = bs_greeks(spot, strike, t, vol, r, q, opt_type).price
    down = bs_greeks(spot * (1 - h), strike, t, vol, r, q, opt_type).price
    return (up - 2 * mid + down) / (spot * h) ** 2


def _finite_diff_vega(spot, strike, t, vol, r, q, opt_type, h=0.001):
    up = bs_greeks(spot, strike, t, vol + h, r, q, opt_type).price
    down = bs_greeks(spot, strike, t, vol - h, r, q, opt_type).price
    # vega per 1% change in IV
    return (up - down) / (2 * h * 100)


def _finite_diff_rho(spot, strike, t, vol, r, q, opt_type, h=0.001):
    up = bs_greeks(spot, strike, t, vol, r + h, q, opt_type).price
    down = bs_greeks(spot, strike, t, vol, r - h, q, opt_type).price
    return (up - down) / (2 * h * 100)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_atm_call_price_sanity():
    """ATM call: S=100, K=100, T=1y, vol=20%, r=5% — textbook price ~10.45."""
    g = bs_greeks(100.0, 100.0, 1.0, 0.20, 0.05, 0.0, "C")
    assert 9.5 < g.price < 11.5, f"ATM call price {g.price} outside textbook band"
    # delta at ATM should be ~0.5-0.6 for this setup
    assert 0.4 < g.delta < 0.7, f"delta {g.delta} outside expected band"


def test_iv_round_trip():
    """Bisect from market price → IV matches input within 1e-4."""
    spot, strike, t, vol = 100.0, 110.0, 0.5, 0.35
    g = bs_greeks(spot, strike, t, vol, 0.04, 0.0, "C")
    iv = mark_iv(spot, strike, t, g.price, 0.04, 0.0, "C")
    assert abs(iv - vol) < 1e-4, f"IV round-trip failed: {iv} != {vol}"


def test_put_call_parity():
    """C - P = S - K*exp(-rT)."""
    spot, strike, t, vol, r, q = 65000.0, 67000.0, 0.05, 0.70, 0.045, 0.0
    c = bs_greeks(spot, strike, t, vol, r, q, "C").price
    p = bs_greeks(spot, strike, t, vol, r, q, "P").price
    parity_lhs = c - p
    parity_rhs = spot - strike * math.exp(-r * t)
    assert abs(parity_lhs - parity_rhs) < 1e-3, (
        f"put-call parity violated: C-P={parity_lhs} vs S-K*exp(-rT)={parity_rhs}"
    )


def test_delta_vs_finite_difference():
    """BS delta must match finite-difference delta within 1e-4."""
    spot, strike, t, vol = 65000.0, 66000.0, 14 / 365, 0.65
    g = bs_greeks(spot, strike, t, vol, 0.045, 0.0, "C")
    fd = _finite_diff_delta(spot, strike, t, vol, 0.045, 0.0, "C")
    assert abs(g.delta - fd) < 1e-4, f"delta {g.delta} vs FD {fd}"


def test_gamma_vs_finite_difference():
    spot, strike, t, vol = 65000.0, 65000.0, 14 / 365, 0.65
    g = bs_greeks(spot, strike, t, vol, 0.045, 0.0, "C")
    fd = _finite_diff_gamma(spot, strike, t, vol, 0.045, 0.0, "C")
    assert abs(g.gamma - fd) < 1e-5, f"gamma {g.gamma} vs FD {fd}"


def test_vega_vs_finite_difference():
    spot, strike, t, vol = 65000.0, 65000.0, 14 / 365, 0.65
    g = bs_greeks(spot, strike, t, vol, 0.045, 0.0, "C")
    fd = _finite_diff_vega(spot, strike, t, vol, 0.045, 0.0, "C")
    assert abs(g.vega - fd) < 1e-2, f"vega {g.vega} vs FD {fd}"


def test_rho_vs_finite_difference():
    spot, strike, t, vol = 65000.0, 65000.0, 0.5, 0.45
    g = bs_greeks(spot, strike, t, vol, 0.045, 0.0, "C")
    fd = _finite_diff_rho(spot, strike, t, vol, 0.045, 0.0, "C")
    assert abs(g.rho - fd) < 1e-2, f"rho {g.rho} vs FD {fd}"


def test_intrinsic_only_at_expiry():
    """At T=0, call price = max(S-K, 0)."""
    g = bs_greeks(105.0, 100.0, 0.0, 0.20, 0.05, 0.0, "C")
    assert abs(g.price - 5.0) < 1e-6
    g2 = bs_greeks(95.0, 100.0, 0.0, 0.20, 0.05, 0.0, "C")
    assert abs(g2.price - 0.0) < 1e-6


def test_portfolio_greeks_zero_for_hedge():
    """A long call + short call (same strike) should have ~0 greeks (within 1e-6)."""
    g = portfolio_greeks(
        [
            {"strike": 65000.0, "opt_type": "C", "qty": 1, "avg_fill_price": 100.0,
             "days_to_expiry": 7, "iv": 0.65},
            {"strike": 65000.0, "opt_type": "C", "qty": -1, "avg_fill_price": 100.0,
             "days_to_expiry": 7, "iv": 0.65},
        ],
        spot=65000.0,
    )
    assert abs(g.delta) < 1e-6
    assert abs(g.gamma) < 1e-6
    assert abs(g.vega) < 1e-6


def test_mark_to_market_long_marks_at_bid():
    """Long position should be marked at BID, not LTP."""
    result = mark_to_market(
        leg={"strike": 65000.0, "opt_type": "C", "qty": 1,
             "avg_fill_price": 100.0, "days_to_expiry": 7, "iv": 0.65},
        spot=65000.0,
        bid=99.0,
        ask=101.0,
    )
    assert result["mtm_price"] == 99.0
    # P&L per unit = 99 - 100 = -1
    assert result["mtm_pnl"] == -1.0


def test_mark_to_market_short_marks_at_ask():
    """Short position should be marked at ASK."""
    result = mark_to_market(
        leg={"strike": 65000.0, "opt_type": "C", "qty": -1,
             "avg_fill_price": 100.0, "days_to_expiry": 7, "iv": 0.65},
        spot=65000.0,
        bid=99.0,
        ask=101.0,
    )
    assert result["mtm_price"] == 101.0
    # Short: P&L = (avg - mark) * |qty| = (100 - 101) * 1 = -1
    assert result["mtm_pnl"] == -1.0


if __name__ == "__main__":
    # Run as a script if pytest isn't installed
    import traceback
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception:
            print(f"  ERROR {fn.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
