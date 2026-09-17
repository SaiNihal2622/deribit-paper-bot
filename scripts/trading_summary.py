"""Live trading summary for the bot.

Reads the same data the dashboard shows, plus the trade-events log,
to give a clean human-readable picture of what the bot is actually
doing right now.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    paper_state_path = ROOT / "data_cache" / "paper_state.json"
    trades_state_path = ROOT / "data_cache" / "trades_state.json"
    heartbeat_path = ROOT / "data_cache" / "heartbeat.json"

    print("=" * 64)
    print("  Crypto Options Paper Bot — Trading Summary")
    print("=" * 64)

    # Bot heartbeat (most current view)
    if heartbeat_path.exists():
        hb = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        ts = datetime.fromtimestamp(hb["ts"], tz=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        print(f"  Bot heartbeat : {ts.strftime('%Y-%m-%d %H:%M:%S UTC')} ({age:.1f}s ago)")
        print(f"  Cycle          : {hb.get('cycle')}  (mode={hb.get('mode')}, feed={hb.get('feed')})")
        print(f"  WS connected   : {hb.get('ws_connected')}   subscribed channels: {hb.get('ws_subscribed')}")
        print(f"  Open trades    : {hb.get('open_trades')}   Positions: {hb.get('positions')}")
        print(f"  Realized PnL   : ${hb.get('realized_pnl'):+.4f}")
        print(f"  Unrealized PnL : ${hb.get('unrealized_pnl'):+.4f}")
        print(f"  Preset         : {hb.get('preset')}   DVOL/IV-rank from /api/status")
        print()

    # Paper state (account / risk)
    if paper_state_path.exists():
        ps = json.loads(paper_state_path.read_text(encoding="utf-8"))
        cash = ps.get("cash", 0.0)
        # try to find positions / orders summary
        positions = ps.get("positions", {})
        orders = ps.get("orders", {})
        print(f"  Paper cash     : ${cash:,.2f}")
        print(f"  Total positions: {len(positions)}")
        if positions:
            for sym, p in list(positions.items())[:6]:
                print(f"    - {sym:32} qty={p.get('qty', 0):+d} avg={p.get('avg_price', 0):.4f} ltp={p.get('ltp', 0):.4f} pnl=${p.get('pnl', 0):+.4f}")
        print(f"  Pending orders : {len(orders)}")
        print()

    # Trades state (open + closed)
    if trades_state_path.exists():
        ts_data = json.loads(trades_state_path.read_text(encoding="utf-8"))
        all_trades = ts_data.get("trades") or ts_data.get("open_trades") or {}
        open_trades = []
        closed_trades = []
        for tid, t in all_trades.items():
            if t.get("closed_at") or t.get("status") == "closed":
                closed_trades.append((tid, t))
            else:
                open_trades.append((tid, t))

        print(f"  Trade count    : {len(all_trades)} total  ({len(open_trades)} open / {len(closed_trades)} closed)")

        # Date breakdown for open + recently closed
        today = datetime.now(timezone.utc).date().isoformat()
        opened_today = []
        for tid, t in all_trades.items():
            opened = (t.get("opened_at") or "")[:10]
            if opened == today:
                opened_today.append((tid, t))

        print(f"  Opened today   : {len(opened_today)}")
        for tid, t in opened_today[:12]:
            print(f"    - {tid}  {t.get('strategy', '?'):20} {t.get('underlying', '?'):4} credit_target={t.get('target', 0):.4f} stop={t.get('stop', 0):.4f}  opened={t.get('opened_at', '?')}")
        print()

        if open_trades:
            print("  Currently open :")
            for tid, t in open_trades:
                opened = (t.get("opened_at") or "")[:19]
                strat = t.get("strategy", "?")
                und = t.get("underlying", "?")
                tgt = t.get("target", 0)
                stp = t.get("stop", 0)
                legs = t.get("leg_count", 2)
                pnl = t.get("realized_pnl", 0) or 0
                print(f"    - {tid}  {strat:20} {und:4} legs={legs}  tgt={tgt:.4f} stop={stp:.4f}  opened={opened} pnl=${pnl:+.4f}")
            print()

        if closed_trades:
            print("  Closed history :")
            for tid, t in closed_trades[-12:]:
                opened = (t.get("opened_at") or "?")[:19]
                closed = (t.get("closed_at") or "?")[:19]
                pnl = t.get("realized_pnl", 0) or 0
                reason = t.get("close_reason", "?")
                print(f"    - {tid}  {t.get('strategy', '?'):20} {t.get('underlying', '?'):4} opened={opened} closed={closed} pnl=${pnl:+.4f} reason={reason}")
            print()

    # Counts of signals fired (rejected + approved)
    if paper_state_path.exists():
        ps = json.loads(paper_state_path.read_text(encoding="utf-8"))
        signals = ps.get("signals", [])
        total = len(signals)
        if total:
            approved = sum(1 for s in signals if s.get("status") == "approved" or s.get("status") == "executed")
            rejected = sum(1 for s in signals if s.get("status") == "rejected")
            print(f"  Signal log     : {total} signals ({approved} approved / {rejected} rejected)")
            if total > 0:
                first = signals[0].get("time", "?")[:19]
                last = signals[-1].get("time", "?")[:19]
                print(f"  Signal span    : {first} → {last}")
            # top rejection reasons
            from collections import Counter
            reasons = Counter()
            for s in signals:
                r = s.get("reason") or "unknown"
                # truncate long reasons
                reasons[r[:60]] += 1
            print(f"  Top rejection reasons:")
            for r, n in reasons.most_common(5):
                print(f"    {n:6d}x  {r}")
            print()

    print("=" * 64)
    print("  Crypto markets: 24/7/365 — no close. The bot trades continuously.")
    print("  Paper capital: $100,000 (configurable in settings.yaml broker.paper_capital)")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
