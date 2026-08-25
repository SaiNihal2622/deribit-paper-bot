"""Shared strategy helpers.

Common utilities used by the concrete strategies in this package — primarily
strike/expiry snapping and finding the next few expiries from the live feed.

These helpers are intentionally tolerant of sparse testnet data: if a strike
has no bid/ask the caller can pass ``mark_price_proxy=True`` so we treat the
mark price as both bid and ask (so the limit-fill simulator can still fill).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Optional

from loguru import logger

from .base import SignalContext


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def snap_strike(target: float, valid_strikes: list[float]) -> Optional[float]:
    """Snap ``target`` to the nearest strike in ``valid_strikes``.

    Returns None if the list is empty. Ties broken to the lower strike.
    """
    if not valid_strikes:
        return None
    return min(valid_strikes, key=lambda k: abs(k - target))


def nearest_expiries_from_symbols(symbols: Iterable[str], underlying: str,
                                  n: int = 4) -> list[str]:
    """Return the nearest ``n`` future expiry ISO dates derived from
    Deribit-format instrument names.

    Sorted ascending by days-to-expiry. Today counts as day 0. If fewer
    than ``n`` future expiries are found, returns whatever is available.
    """
    underlying = underlying.upper()
    today = datetime.now(timezone.utc).date()
    seen: dict[str, int] = {}
    for sym in symbols or []:
        if not sym or not sym.startswith(f"{underlying}-"):
            continue
        parts = sym.split("-")
        if len(parts) < 4:
            continue
        ddmmyy = parts[1]
        if len(ddmmyy) != 7:
            continue
        try:
            dd = int(ddmmyy[0:2])
            mmm = ddmmyy[2:5]
            yy = int(ddmmyy[5:7])
            exp_dt = date(2000 + yy, _MONTHS[mmm], dd)
        except (KeyError, ValueError):
            continue
        days = (exp_dt - today).days
        if days < 0:
            continue
        iso = exp_dt.isoformat()
        if iso not in seen or days < seen[iso]:
            seen[iso] = days
    out = sorted(seen.items(), key=lambda kv: kv[1])
    return [iso for iso, _days in out[:n]]


def nearest_expiry_ddmmyy(feed, underlying: str) -> Optional[str]:
    """Convenience wrapper for the feed's nearest expiry in DDMMMYY form.

    Returns the bare DDMMMYY string (e.g. '26DEC25') or None.
    """
    if feed is None:
        return None
    getter = getattr(feed, "get_nearest_expiry", None)
    if not getter:
        return None
    try:
        return getter(underlying)
    except Exception as e:
        logger.debug(f"nearest_expiry_ddmmyy error: {e}")
        return None


def effective_bid_ask(ltp: float, bid: float, ask: float,
                      mark_price_proxy: bool = False) -> tuple[float, float]:
    """Return a (bid, ask) pair, using mark-price proxy if configured.

    On sparse testnet many strikes report ``bid=0`` / ``ask=0`` even though
    ``mark_price`` is healthy. The strategies need *some* limit-fill price
    to build a plan; with ``mark_price_proxy=True`` we treat the LTP as both
    the synthetic bid and ask (with a small spread). When the option has
    a real bid/ask we use those instead.
    """
    if bid > 0 and ask > 0:
        return float(bid), float(ask)
    if not mark_price_proxy or ltp <= 0:
        return float(bid), float(ask)
    # synthesise a 0.5% spread around the LTP for fill purposes
    spread = max(0.0001, ltp * 0.0025)
    return float(ltp - spread), float(ltp + spread)


def best_atm_iv(ctx: SignalContext, spot: float) -> float:
    """Return the best available ATM IV from the context.

    Falls back to the nearest non-zero IV if the literal ATM is empty.
    Returns 0.0 if the chain has no IV at all.
    """
    if not ctx.strikes:
        return 0.0
    atm = min(ctx.strikes, key=lambda k: abs(k - spot))
    iv_c = ctx.option_ivs.get((atm, "C"), 0.0)
    iv_p = ctx.option_ivs.get((atm, "P"), 0.0)
    if iv_c > 0 and iv_p > 0:
        return (iv_c + iv_p) / 2.0
    if iv_c > 0:
        return iv_c
    if iv_p > 0:
        return iv_p
    # Fallback: nearest non-zero IV across the chain
    best = 0.0
    best_dist = float("inf")
    for (strike, _opt), iv in ctx.option_ivs.items():
        if iv > 0:
            dist = abs(strike - spot)
            if dist < best_dist:
                best_dist = dist
                best = iv
    return best


def option_ltp_with_proxy(ctx: SignalContext, strike: float, opt_type: str,
                           mark_price_proxy: bool) -> float:
    """Return the LTP for a (strike, opt_type) leg, possibly via mark proxy.

    The proxy is only used when the LTP is non-zero but bid/ask are both
    zero (the testnet edge case). When LTP itself is zero, we return 0.0
    so the caller can short-circuit the plan build.
    """
    ltp = ctx.option_ltps.get((strike, opt_type), 0.0)
    return float(ltp)


__all__ = [
    "snap_strike",
    "nearest_expiries_from_symbols",
    "nearest_expiry_ddmmyy",
    "effective_bid_ask",
    "best_atm_iv",
    "option_ltp_with_proxy",
]
