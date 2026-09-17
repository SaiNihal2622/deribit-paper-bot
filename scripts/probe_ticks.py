"""Quick probe: print all subscribed BTC/ETH option strikes with their
bid/ask/ltp/iv so we can see what's actually populated vs 0 on testnet."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    for currency in ("BTC", "ETH"):
        url = f"http://127.0.0.1:8511/api/ticks?currency={currency}"
        r = urllib.request.urlopen(url, timeout=5)
        ticks = json.loads(r.read()).get("ticks", [])
        print(f"\n{currency}: {len(ticks)} ticks")
        zero_ltp = sum(1 for t in ticks if float(t.get("ltp") or 0) == 0)
        nonzero_ltp = sum(1 for t in ticks if float(t.get("ltp") or 0) > 0)
        nonzero_iv = sum(1 for t in ticks if float(t.get("iv") or 0) > 0)
        print(f"  LTP=0: {zero_ltp}    LTP>0: {nonzero_ltp}    IV>0: {nonzero_iv}")

        # Sort by strike for readability.
        def strike_of(t):
            sym = t.get("symbol", "")
            parts = sym.split("-")
            if len(parts) >= 4:
                try:
                    return int(parts[3])
                except ValueError:
                    return 0
            return 0

        for t in sorted(ticks, key=strike_of):
            sym = t.get("symbol", "?")
            ltp = float(t.get("ltp") or 0)
            bid = float(t.get("bid") or 0)
            ask = float(t.get("ask") or 0)
            iv = float(t.get("iv") or 0)
            mark = "  " if ltp > 0 else ("IV" if iv > 0 else "..")
            print(f"  [{mark}] {sym:32} ltp={ltp:.4f} bid={bid:.4f} ask={ask:.4f} iv={iv:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
