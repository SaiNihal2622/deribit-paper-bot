"""Black-Scholes option Greeks — delta, gamma, vega, theta, rho.

Pure-Python implementation, no external dependencies (no scipy). Uses the
standard normal CDF and PDF computed via the error function (math.erf).

Crypto options on Deribit settle in the underlying (inverse contracts), so:
  - r = risk-free rate (default 4.5% — US 10Y proxy, since crypto is global)
  - q = continuous dividend yield (0.0 for BTC/ETH — no native yield assumed)
  - option_type: "C" / "P" (Deribit single-letter). We also tolerate "CE" / "PE".

The Greeks here are used for:
  - Risk management: how much will the position move if spot moves 1%?
  - Position sizing: how many contracts to keep delta-neutral?
  - Exit logic: did theta decay kill the trade's edge?
  - Mark-to-market with bid/ask spread (using delta to estimate fair value
    after a 1-tick move).

Conventions:
  - All inputs in YEARS (e.g. 7 days = 7/365)
  - All outputs per-unit (multiply by qty * contract_size for portfolio impact)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from loguru import logger


SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _is_call(option_type: str) -> bool:
    """Tolerate "C", "CE", "call" etc. — anything starting with C is a call."""
    return option_type.upper().startswith("C")


@dataclass
class Greeks:
    """All first- and second-order Greeks for a single option contract."""

    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0       # per 1% change in IV
    theta: float = 0.0      # per day
    rho: float = 0.0        # per 1% change in r
    price: float = 0.0      # theoretical price
    d1: float = 0.0
    d2: float = 0.0

    def to_dict(self) -> dict:
        return {
            "delta": round(self.delta, 6),
            "gamma": round(self.gamma, 6),
            "vega": round(self.vega, 6),
            "theta": round(self.theta, 6),
            "rho": round(self.rho, 6),
            "price": round(self.price, 4),
        }


def bs_greeks(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    vol: float,
    r: float = 0.045,
    q: float = 0.0,
    option_type: Literal["C", "P", "CE", "PE"] = "C",
) -> Greeks:
    """Compute Black-Scholes Greeks for a European option.

    Args:
        spot: current underlying price (e.g. 65000.0 for BTC)
        strike: option strike price
        time_to_expiry_years: T in years (e.g. 7/365 for a 7-day option)
        vol: annualized implied volatility (e.g. 0.65 for 65%)
        r: risk-free rate (annualized; default 4.5% — US 10Y)
        q: continuous dividend yield (annualized; 0.0 for BTC/ETH)
        option_type: 'C' / 'P' (Deribit) or 'CE' / 'PE' (NSE-style)

    Returns:
        Greeks dataclass with delta, gamma, vega, theta, rho, price
    """
    if vol <= 0 or time_to_expiry_years <= 0 or spot <= 0 or strike <= 0:
        # Edge case: at expiry or invalid inputs — return intrinsic-only values
        if _is_call(option_type):
            return Greeks(
                delta=1.0 if spot > strike else 0.0,
                price=max(0.0, spot - strike),
            )
        return Greeks(
            delta=-1.0 if spot < strike else 0.0,
            price=max(0.0, strike - spot),
        )

    sqrt_t = math.sqrt(time_to_expiry_years)
    sigma_sqrt_t = vol * sqrt_t
    if sigma_sqrt_t == 0:
        sigma_sqrt_t = 1e-9
    log_moneyness = math.log(spot / strike)
    d1 = (log_moneyness + (r - q + 0.5 * vol * vol) * time_to_expiry_years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t

    is_call = _is_call(option_type)
    if is_call:
        price = (
            spot * math.exp(-q * time_to_expiry_years) * _norm_cdf(d1)
            - strike * math.exp(-r * time_to_expiry_years) * _norm_cdf(d2)
        )
        delta = math.exp(-q * time_to_expiry_years) * _norm_cdf(d1)
        theta = (
            -(spot * _norm_pdf(d1) * vol * math.exp(-q * time_to_expiry_years)) / (2.0 * sqrt_t)
            - r * strike * math.exp(-r * time_to_expiry_years) * _norm_cdf(d2)
            + q * spot * math.exp(-q * time_to_expiry_years) * _norm_cdf(d1)
        ) / 365.0  # per day
        rho = (strike * time_to_expiry_years * math.exp(-r * time_to_expiry_years) * _norm_cdf(d2)) / 100.0
    else:
        price = (
            strike * math.exp(-r * time_to_expiry_years) * _norm_cdf(-d2)
            - spot * math.exp(-q * time_to_expiry_years) * _norm_cdf(-d1)
        )
        delta = -math.exp(-q * time_to_expiry_years) * _norm_cdf(-d1)
        theta = (
            -(spot * _norm_pdf(d1) * vol * math.exp(-q * time_to_expiry_years)) / (2.0 * sqrt_t)
            + r * strike * math.exp(-r * time_to_expiry_years) * _norm_cdf(-d2)
            - q * spot * math.exp(-q * time_to_expiry_years) * _norm_cdf(-d1)
        ) / 365.0  # per day
        rho = (-strike * time_to_expiry_years * math.exp(-r * time_to_expiry_years) * _norm_cdf(-d2)) / 100.0

    gamma = (math.exp(-q * time_to_expiry_years) * _norm_pdf(d1)) / (spot * sigma_sqrt_t)
    # Vega per 1% change in IV (so divide by 100)
    vega = (spot * math.exp(-q * time_to_expiry_years) * _norm_pdf(d1) * sqrt_t) / 100.0

    return Greeks(
        delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho,
        price=max(0.0, price), d1=d1, d2=d2,
    )


def mark_iv(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    market_price: float,
    r: float = 0.045,
    q: float = 0.0,
    option_type: str = "C",
    tol: float = 1e-4,
    max_iter: int = 60,
) -> float:
    """Solve for implied volatility from a market price using bisection.

    Use as a fallback when Deribit's mark_iv is not available. Returns
    annualized vol (e.g. 0.65 for 65%). Clamped to [1%, 300%].
    """
    if spot <= 0 or strike <= 0 or time_to_expiry_years <= 0 or market_price <= 0:
        return 0.0
    lo, hi = 0.01, 3.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        g = bs_greeks(spot, strike, time_to_expiry_years, mid, r, q, option_type)
        diff = g.price - market_price
        if abs(diff) < tol:
            return mid
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def _resolve_iv(leg: dict, spot: float, r: float, q: float) -> float:
    """Resolve IV for a leg: override > live feed IV > bisect from fill."""
    iv = float(leg.get("iv_override", 0) or 0)
    if iv <= 0:
        iv = float(leg.get("iv", 0) or 0)
    if iv <= 0:
        fill = float(leg.get("avg_fill_price", 0) or 0)
        days = float(leg.get("days_to_expiry", 7))
        t = max(1e-6, days / 365.0)
        strike = float(leg.get("strike", 0))
        opt = leg.get("opt_type", leg.get("option_type", "C"))
        if fill > 0 and spot > 0 and strike > 0:
            iv = mark_iv(spot, strike, t, fill, r, q, opt)
    if iv <= 0:
        iv = 0.20
    return max(0.05, min(3.0, iv))


def mark_to_market(
    leg: dict,
    spot: float,
    bid: float,
    ask: float,
    iv: float = 0.0,
    r: float = 0.045,
    q: float = 0.0,
) -> dict:
    """Mark a leg to market using the conservative side of the bid/ask spread.

    Long positions are marked at BID (what you'd get if you sold to close).
    Short positions are marked at ASK (what you'd pay to buy to close).

    If `iv` > 0 it is used directly (preferred — Deribit's mark_iv); otherwise
    the function bisects from the fill price.

    Returns:
        dict with: theoretical_price, iv, bid, ask, mtm_price, mtm_pnl,
                   mid_spread_bps, delta
    """
    strike = float(leg.get("strike", 0))
    opt = leg.get("opt_type", leg.get("option_type", "C"))
    qty = int(leg.get("qty", 0))
    fill = float(leg.get("avg_fill_price", 0) or 0)
    days = float(leg.get("days_to_expiry", 7))
    t = max(1e-6, days / 365.0)

    # IV resolution: explicit iv arg > iv_override > live feed IV > bisect
    if iv <= 0:
        iv = _resolve_iv(leg, spot, r, q)

    g = bs_greeks(spot, strike, t, iv, r, q, opt)

    if bid > 0 and ask > 0:
        is_long = qty > 0
        mtm_unit = bid if is_long else ask
    else:
        mtm_unit = g.price
    mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else g.price
    spread_bps = ((ask - bid) / mid * 10000) if (mid > 0 and ask > 0 and bid > 0) else 0.0
    pnl_per_unit = mtm_unit - fill
    sign = 1.0 if qty > 0 else -1.0
    pnl = pnl_per_unit * abs(qty) * sign
    return {
        "theoretical_price": round(g.price, 4),
        "iv": round(iv, 4),
        "bid": bid,
        "ask": ask,
        "mtm_price": round(mtm_unit, 4),
        "mtm_pnl": round(pnl, 2),
        "mid_spread_bps": round(spread_bps, 1),
        "delta": round(g.delta, 4),
    }


def portfolio_greeks(
    legs: list[dict],
    spot: float,
    r: float = 0.045,
    q: float = 0.0,
) -> Greeks:
    """Aggregate Greeks across a multi-leg position.

    Each leg dict must have:
        strike, opt_type ('C'/'P' or 'CE'/'PE'), qty (+long, -short),
        avg_fill_price (for IV estimation), days_to_expiry (default 7).
    Optional: iv_override, iv (live feed IV — e.g. Deribit mark_iv).
    """
    total = Greeks()
    for leg in legs:
        strike = float(leg.get("strike", 0))
        opt = leg.get("opt_type", leg.get("option_type", "C"))
        qty = int(leg.get("qty", 0))
        days = float(leg.get("days_to_expiry", 7))
        t = max(1e-6, days / 365.0)
        iv = _resolve_iv(leg, spot, r, q)
        g = bs_greeks(spot, strike, t, iv, r, q, opt)
        sign = 1.0 if qty > 0 else -1.0
        abs_qty = abs(qty)
        total.delta += sign * g.delta * abs_qty
        total.gamma += sign * g.gamma * abs_qty
        total.vega += sign * g.vega * abs_qty
        total.theta += sign * g.theta * abs_qty
        total.rho += sign * g.rho * abs_qty
        total.price += sign * g.price * abs_qty
    return total
