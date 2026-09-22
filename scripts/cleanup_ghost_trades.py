"""One-shot cleanup: mark all currently-open ghost trades as closed.

Background: when the dedupe bug was active (2026-09-22 ~16:27-16:54 IST),
the bot over-fired ETH short_strangles in quick succession. The bot was
then restarted before paper_state.json was flushed, so the broker side
resets to 0 positions while the OrderManager journal still has 6 "open"
trade entries — ghost entries with no real broker exposure.

This script reads data_cache/trades_state.json, sets closed_at on every
trade that doesn't have one yet, and writes the file back atomically
(uses the same unique-tmp + retry pattern the broker saves now use).

Usage:
    python scripts/cleanup_ghost_trades.py            # clean
    python scripts/cleanup_ghost_trades.py --dry-run  # show what would change

After running, restart the bot so it reloads the journal:
    Stop the bot, then `start_bot.bat` or
    Start-Process python -m crypto_options_bot paper ...
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADES_STATE = PROJECT_ROOT / "data_cache" / "trades_state.json"
BACKUP_PATH = PROJECT_ROOT / "data_cache" / f"trades_state.ghost-cleanup-{int(time.time())}.bak"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomic JSON write — unique tmp + retry + fallback. Mirrors paper_client._save_state."""
    json_text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    unique_tmp = path.parent / (
        f".trades_state.{os.getpid()}.{int(time.time() * 1000) % 100000}.tmp"
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
                # Last-ditch fallback path
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
    p = argparse.ArgumentParser(description="Mark ghost-trades as closed.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing.",
    )
    args = p.parse_args()

    if not TRADES_STATE.exists():
        print(f"ERROR: {TRADES_STATE} not found. Nothing to clean.", file=sys.stderr)
        return 1

    with TRADES_STATE.open("r", encoding="utf-8") as f:
        state = json.load(f)

    trades: dict = state.get("trades", {})
    sym_to_trade: dict = state.get("symbol_to_trade", {})

    if not trades:
        print("No trades in journal — already clean.")
        return 0

    now = _now_iso()
    ghost_ids: list[str] = []
    already_closed = 0
    touched_symbols: list[str] = []

    for tid, t in trades.items():
        if t.get("closed_at"):
            already_closed += 1
            continue
        if args.dry_run:
            print(f"  WOULD CLOSE: {tid} opened_at={t.get('opened_at')} underlying={t.get('plan', {}).get('underlying', '?')}")
        else:
            t["closed_at"] = now
            t["exit_reason"] = "ghost_cleanup_2026-09-22"
        ghost_ids.append(tid)

    # Clean symbol_to_trade entries pointing to closed trades
    if not args.dry_run:
        for sym, tid in list(sym_to_trade.items()):
            if tid in ghost_ids:
                del sym_to_trade[sym]
                touched_symbols.append(sym)
    else:
        for sym, tid in sym_to_trade.items():
            if tid in ghost_ids:
                print(f"  WOULD REMOVE symbol_to_trade['{sym}'] -> {tid}")

    print()
    print(f"Summary:")
    print(f"  Total trades in journal : {len(trades)}")
    print(f"  Already closed          : {already_closed}")
    print(f"  Ghost (will be closed)  : {len(ghost_ids)}")
    print(f"  Symbol mappings cleaned : {len(touched_symbols)}")

    if args.dry_run:
        print()
        print("Dry-run only — no changes written. Re-run without --dry-run to apply.")
        return 0

    # Backup before mutating
    shutil.copy2(TRADES_STATE, BACKUP_PATH)
    print(f"  Backup saved to         : {BACKUP_PATH}")

    # Atomic write
    state["trades"] = trades
    state["symbol_to_trade"] = sym_to_trade
    _atomic_write_json(TRADES_STATE, state)
    print(f"  Updated                 : {TRADES_STATE}")

    print()
    print("Done. Restart the bot so it reloads the cleaned journal:")
    print("  scripts\\start_bot.bat")
    print()
    print("(Or kill the running bot and re-launch via Start-Process python -m crypto_options_bot paper …)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
