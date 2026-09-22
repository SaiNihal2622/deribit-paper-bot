"""One-shot rebuild: derive broker positions from journal orders.

Root cause this addresses
-------------------------
On every bot restart the in-memory broker state is lost; only the
periodic heartbeat-save (every ~30s) writes `paper_state.json`. If the
bot restarts between a trade being placed and the next save, the broker
ends up with `positions={}` while the OrderManager journal still has the
trade as open. This script derives positions from the journal's filled
orders and patches `paper_state.json` so they reconcile.

Effect: next bot load_state() reads the rebuilt positions, mark-to-market
works correctly, and at expiry the closing leg fills with the right P&L.

Usage
-----
    python scripts/rebuild_broker_positions.py            # rebuild + write
    python scripts/rebuild_broker_positions.py --dry-run  # show what would change
    python scripts/rebuild_broker_positions.py --force    # overwrite non-empty broker positions

After running, restart the bot:
    scripts\\start_bot.bat
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADES_STATE = PROJECT_ROOT / "data_cache" / "trades_state.json"
PAPER_STATE = PROJECT_ROOT / "data_cache" / "paper_state.json"
BACKUP_PATH = PAPER_STATE.with_suffix(
    f".pre-rebuild-{int(time.time())}.bak"
)


_DDMMMYY_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_symbol(symbol: str) -> dict:
    """Parse a Deribit symbol like 'ETH-23SEP26-2500-C' into parts.

    Returns dict with keys: underlying, expiry, strike, option_type.
    `expiry` is in DDMMMYY form (broker canonical); the bot also keeps
    an ISO date in `order.expiry`, but the symbol is authoritative.
    """
    parts = symbol.split("-")
    if len(parts) != 4:
        return {}
    return {
        "underlying": parts[0],
        "expiry": parts[1],          # DDMMMYY
        "strike": float(parts[2]),
        "option_type": parts[3],
    }


def _is_complete(order: dict) -> bool:
    """An order counts toward positions if it actually filled."""
    return (
        (order.get("status") or "").lower() in {"complete", "filled"}
        and (order.get("filled_qty") or 0) > 0
    )


def _derive_positions(trades: dict) -> dict:
    """Walk all open trades and aggregate net positions per symbol.

    For each complete order:
      - side SELL  -> signed qty = -filled_qty
      - side BUY   -> signed qty = +filled_qty
    We then aggregate per symbol and compute volume-weighted avg fill.

    Returns dict {symbol: position_dict} matching PaperClient's expected
    in-memory schema (which is what gets serialised to paper_state.json).
    """
    # symbol -> list of (signed_qty, fill_price, order_meta)
    contribs: dict[str, list[tuple[float, float, dict]]] = defaultdict(list)

    for tid, trade in trades.items():
        closed_at = trade.get("closed_at")
        if closed_at:
            continue
        for order in trade.get("orders") or []:
            if not _is_complete(order):
                continue
            side = (order.get("side") or "").upper()
            filled_qty = int(order.get("filled_qty") or 0)
            if side == "SELL":
                signed = -filled_qty
            elif side == "BUY":
                signed = +filled_qty
            else:
                continue
            symbol = order["symbol"]
            price = float(order.get("avg_fill_price") or order.get("price") or 0.0)
            contribs[symbol].append((signed, price, order))

    out: dict[str, dict] = {}
    for symbol, items in contribs.items():
        net_qty = sum(s for s, _p, _o in items)
        # Volume-weighted average fill price over absolute quantities
        # (so a -5@0.09 + +3@0.10 averages to (5*0.09 + 3*0.10) / 8 = 0.09375,
        # not zero-weight-negative).
        total_abs = sum(abs(s) for s, _p, _o in items)
        if total_abs <= 0:
            continue
        vwap = sum(abs(s) * p for s, p, _o in items) / total_abs
        # Take metadata from the most recent fill
        latest = max(items, key=lambda t: t[2].get("filled_at") or "")[2]
        parsed = _parse_symbol(symbol)
        # Order.expiry is ISO date; symbol has DDMMMYY. Prefer ISO when present.
        iso_expiry = latest.get("expiry") or ""
        out[symbol] = {
            "symbol": symbol,
            "qty": net_qty,
            "avg_price": round(vwap, 6),
            "ltp": round(vwap, 6),
            "exchange": latest.get("exchange") or "DERIBIT",
            "pnl": 0.0,
            "strike": latest.get("strike") or parsed.get("strike") or 0.0,
            "option_type": latest.get("option_type") or parsed.get("option_type"),
            "expiry": iso_expiry or parsed.get("expiry") or "",
            "underlying": latest.get("underlying") or parsed.get("underlying") or "",
            "contract_size": 1.0,
            "entry_time": latest.get("filled_at") or _now_iso(),
        }
    return out


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomic JSON write — unique tmp + retry + fallback (matches
    paper_client._save_state and order_manager._save_state patterns)."""
    json_text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    unique_tmp = path.parent / (
        f".rebuild.{os.getpid()}.{int(time.time() * 1000) % 100000}.tmp"
    )
    fixed_tmp = path.with_suffix(".tmp")
    for attempt in range(5):
        try:
            unique_tmp.write_text(json_text, encoding="utf-8")
            if path.exists():
                os.replace(unique_tmp, path)
            else:
                unique_tmp.replace(path)
            if fixed_tmp.exists() and fixed_tmp != unique_tmp:
                try:
                    fixed_tmp.unlink()
                except OSError:
                    pass
            return
        except (PermissionError, OSError):
            if attempt < 4:
                time.sleep(0.1 * (attempt + 1))
            else:
                try:
                    fixed_tmp.write_text(json_text, encoding="utf-8")
                    if path.exists():
                        os.replace(fixed_tmp, path)
                    else:
                        fixed_tmp.replace(path)
                    return
                except Exception as exc:
                    raise RuntimeError(f"failed to write {path}: {exc}") from exc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change without writing.")
    p.add_argument("--force", action="store_true",
                   help="Overwrite paper_state.json's positions even when "
                        "it already has non-empty positions.")
    args = p.parse_args()

    if not TRADES_STATE.exists():
        print(f"ERROR: {TRADES_STATE} not found.", file=sys.stderr)
        return 1
    if not PAPER_STATE.exists():
        print(f"ERROR: {PAPER_STATE} not found.", file=sys.stderr)
        return 1

    with TRADES_STATE.open("r", encoding="utf-8") as f:
        ts = json.load(f)
    with PAPER_STATE.open("r", encoding="utf-8") as f:
        ps = json.load(f)

    trades = ts.get("trades") or {}
    cur_positions = ps.get("positions") or {}

    # Open trades only
    open_trades = [t for t in trades.values() if not t.get("closed_at")]
    closed_trades = [t for t in trades.values() if t.get("closed_at")]
    print(f"Journal: {len(open_trades)} open, {len(closed_trades)} closed")

    derived = _derive_positions(trades)
    print(f"Derived positions from journal: {len(derived)} symbols")
    if derived:
        for sym, p in sorted(derived.items()):
            print(f"  {sym}: qty={p['qty']:+d} avg={p['avg_price']:.4f} "
                  f"strike={p['strike']} {p['option_type']} "
                  f"exp={p['expiry']}")

    # Decide whether to apply
    if cur_positions and not args.force:
        print()
        print(f"Broker already has {len(cur_positions)} positions:")
        for sym, p in cur_positions.items():
            print(f"  {sym}: qty={p.get('qty', '?')}")
        print()
        print("Refusing to overwrite without --force. Run with --force "
              "to replace the existing broker positions with the derived set.")
        return 2

    if args.dry_run:
        print()
        print("Dry-run only. No changes written.")
        return 0

    # Backup before mutating
    shutil.copy2(PAPER_STATE, BACKUP_PATH)
    print(f"\nBackup: {BACKUP_PATH}")

    # Patch and atomic-write
    ps["positions"] = derived
    _atomic_write_json(PAPER_STATE, ps)
    print(f"Patched: {PAPER_STATE} (positions={len(derived)})")
    print()
    print("Restart the bot to load the rebuilt positions:")
    print("  scripts\\start_bot.bat")
    print()
    print("Verify after restart:")
    print("  python -m crypto_options_bot status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
