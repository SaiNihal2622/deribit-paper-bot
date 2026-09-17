"""Operator — pretty-print current status for humans / Telegram.

Reads ``memory/state/health:latest``, ``operator.heartbeat``, and the
Operator's own journal to produce a one-shot status snapshot. Safe to
run anytime; never modifies state.

Usage:
    python scripts/operator_status.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY = PROJECT_ROOT / "memory"
HEARTBEAT = PROJECT_ROOT / "data_cache" / "operator.heartbeat"


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    print("=" * 60)
    print(f"  Crypto Options Bot — Operator Status")
    print("=" * 60)

    hb = read_json(HEARTBEAT)
    if not hb:
        print("  Heartbeat: missing — operator may not be running")
        print()
        print("  Start it:  powershell -File scripts/start_operator.ps1")
        return 1

    uptime = time.time() - hb.get("ts", 0)
    print(f"  Cycles:          {hb.get('cycles', '?')}")
    print(f"  Heartbeat age:   {uptime:6.1f}s  (PID={hb.get('pid', '?')})")
    print()
    print("  Scheduled jobs:")
    for j in hb.get("scheduler", []):
        flag = "PAUSED" if j["paused"] else "     "
        next_in = j["next_run_in_sec"]
        print(
            f"    {flag} {j['name']:<10} "
            f"runs={j['runs']:<4} errors={j['errors']:<3} "
            f"next={next_in}s"
        )
    print()

    # Latest health from Sentinel.
    health = read_json(MEMORY / "state" / "health_latest.json")
    if health:
        print("  Latest Sentinel probe:")
        flags = []
        if not health.get("bot_alive"):
            flags.append("BOT-DEAD")
        ha = health.get("heartbeat_age_sec")
        if ha is not None and ha > 120:
            flags.append(f"heartbeat-stale({int(ha)}s)")
        ws = health.get("ws_subscribed", 0)
        if ws == 0:
            flags.append("ws-no-channels")
        if not flags:
            flags.append("ok")
        print(f"    {','.join(flags)} pos={health.get('open_positions', '?')} "
              f"trades={health.get('open_trades', '?')} ws={ws}")
        notes = health.get("notes") or []
        for n in notes[:3]:
            print(f"      note: {n}")
    else:
        print("  Latest Sentinel probe: <none yet>")

    # Latest healer action.
    healer = read_json(MEMORY / "state" / "healer_latest.json")
    if healer and healer.get("actions"):
        print()
        print("  Latest Healer run:")
        for a in healer["actions"][:5]:
            acted = "✓" if a["acted"] else "·"
            print(
                f"    {acted} {a['playbook']:<18} "
                f"sev={a['severity']:<7} {a['detail'][:60]}"
            )

    # Pending evolver proposals.
    proposals_dir = MEMORY / "proposals"
    pending = []
    if proposals_dir.exists():
        for p in sorted(proposals_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if data.get("status") == "pending":
                pending.append(data)
    if pending:
        print()
        print(f"  Pending Evolver proposals ({len(pending)}):")
        for p in pending[:5]:
            print(f"    - {p['id']:<22} risk={p['risk']:<5} {p['summary'][:80]}")

    # Lessons written recently.
    lessons_dir = MEMORY / "lessons"
    if lessons_dir.exists():
        lessons = sorted(lessons_dir.glob("*.md"), reverse=True)[:3]
        if lessons:
            print()
            print("  Recent Reflector lessons:")
            for lp in lessons:
                print(f"    - {lp.name}")

    print()
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
