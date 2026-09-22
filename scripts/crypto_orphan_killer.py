"""crypto_orphan_killer.py — kill zombie crypto_options_bot python processes.

Mirrors kotak-neo-bot/scripts/orphan_killer.py. The orphan pattern:
  - NSSM starts a python child as SYSTEM
  - The child dies (kill, crash, OOM)
  - NSSM tries to restart, but Windows holds the file handle on
    data_cache/heartbeat.json / liveness.json
  - A new bot starts with a fresh PID, BUT the old python process is still
    alive (orphaned from its nssm wrapper) with empty cmdline metadata
  - The orphan keeps writing stale heartbeat.json with old pid, breaking
    any health probe

This script uses wmic-style enumeration via PowerShell's Get-CimInstance
(works from SYSTEM context without psutil) to find all python processes
matching crypto_options_bot patterns, then kills any whose PID is not the
"official" live PID. Called by supervisor_loop.ps1 and crypto_supervisor.py
on every supervisor cycle (with throttling).

Usage:
  python scripts/crypto_orphan_killer.py            # dry-run: list orphans
  python scripts/crypto_orphan_killer.py --clean    # actually kill them
  python scripts/crypto_orphan_killer.py --force    # kill ALL matching
                                                    # (use after a planned restart)

Exit codes:
  0 = no orphans
  1 = orphans found / killed
  2 = script error
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.resolve()
LOG = ROOT / "logs" / "orphan_killer.log"
DCACHE = ROOT / "data_cache"
LIVENESS = DCACHE / "liveness.json"

# Cmdline patterns that identify a "crypto_options_bot trading loop".
# Match the actual python -m crypto_options_bot paper invocation, NOT
# other python invocations (Telegram alerter, dashboard, etc).
BOT_PATTERNS = (
    "-m crypto_options_bot paper",
    "crypto_options_bot\\__main__",
)


def _log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _live_pid() -> int | None:
    """Read the official live PID from liveness.json. None if missing/invalid."""
    if not LIVENESS.exists():
        return None
    try:
        d = json.loads(LIVENESS.read_text(encoding="utf-8"))
        pid = d.get("pid")
        return int(pid) if pid else None
    except Exception:
        return None


def _list_bot_processes() -> list[tuple[int, str]]:
    """Return [(pid, cmdline)] for python processes matching BOT_PATTERNS.

    Uses PowerShell's Get-CimInstance Win32_Process (works from SYSTEM
    context without psutil). Output is tab-separated: pid<TAB>cmdline.
    """
    try:
        # Use single quotes inside the outer double-quoted arg to avoid
        # PowerShell interpolation of $_.ProcessId etc.
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
            "| Select-Object ProcessId,CommandLine "
            "| ForEach-Object { "
            "  $_.ProcessId.ToString() + \"`t\" + $_.CommandLine "
            "}"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        out = []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or "\t" not in line:
                continue
            parts = line.split("\t", 1)
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            cmd = parts[1] if len(parts) > 1 else ""
            out.append((pid, cmd))
        return out
    except Exception as e:
        _log(f"  _list_bot_processes failed: {e}")
        return []


def _kill(pid: int, force: bool = True) -> bool:
    try:
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"]
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        # Give Windows a moment to release the handle
        import time
        time.sleep(0.5)
        # Verify
        r2 = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Get-Process -Id {pid} -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=5,
        )
        gone = (r2.stdout or "").strip() == ""
        return gone
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true", help="Kill detected orphans")
    parser.add_argument("--force", action="store_true", help="Kill ALL matching (no live-pid filter)")
    args = parser.parse_args()

    live_pid = _live_pid()
    procs = _list_bot_processes()
    bot_procs = [(p, c) for (p, c) in procs if any(pat in c for pat in BOT_PATTERNS)]

    _log(f"live_pid={live_pid} matched={len(bot_procs)}/{len(procs)}")

    if not bot_procs:
        _log("  no bot processes; nothing to do")
        return 0

    # Decide what to kill
    if args.force:
        targets = bot_procs
    else:
        targets = [(p, c) for (p, c) in bot_procs if p != live_pid]

    if not targets:
        _log("  no orphans")
        return 0

    _log(f"  found {len(targets)} orphan(s):")
    for p, c in targets:
        short_cmd = c[:80].replace("\n", " ")
        _log(f"    pid={p} cmd={short_cmd!r}")

    if not args.clean:
        _log("  (dry-run — pass --clean to kill)")
        return 1

    killed = 0
    for p, c in targets:
        if _kill(p):
            _log(f"    killed pid={p}")
            killed += 1
        else:
            _log(f"    [warn] failed to kill pid={p}")

    _log(f"done: killed {killed}/{len(targets)}")
    return 0 if killed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
