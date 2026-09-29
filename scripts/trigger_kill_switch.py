"""trigger_kill_switch.py — manual panic button.

Drops a file at data_cache/kill_switch.json that the bot's main loop
detects on its next cycle. The bot then:
  1. Cancels all open orders via broker.cancel_all_open_orders()
  2. Closes all positions via broker.close_all_positions()
  3. Pauses the risk engine so no new trades fire
  4. Renames the file to .consumed-<ts> so it doesn't fire again

Use when:
  - Market is moving against the bot (large drawdown detected)
  - You suspect a bug or runaway behavior
  - Manual override before going to bed / leaving the bot unattended

Usage:
    python scripts/trigger_kill_switch.py [--reason "text"] [--resume]

The bot picks it up on its next cycle (within ~5s) and unwinds.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger the bot kill switch.")
    parser.add_argument(
        "--reason", default="manual",
        help="Why are you triggering the kill switch? (recorded in audit log)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Clear any pending kill_switch.json (resume without triggering)",
    )
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    ks = repo / "data_cache" / "kill_switch.json"

    if args.resume:
        if ks.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            target = ks.with_suffix(ks.suffix + f".cleared-{ts}")
            ks.rename(target)
            print(f"Cleared kill_switch.json -> {target.name}")
        else:
            print("No kill_switch.json to clear")
        return 0

    ks.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": args.reason,
        "triggered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "triggered_by": "manual",
    }
    # Atomic write so the bot doesn't read a partial file
    tmp = ks.with_suffix(ks.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(ks)
    print(f"Kill switch armed: {ks}")
    print(f"Reason: {args.reason}")
    print("The bot will detect this on its next cycle (~5s) and unwind all positions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
