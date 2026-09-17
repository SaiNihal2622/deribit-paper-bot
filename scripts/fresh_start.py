"""Fresh start: clear paper state + memory + bot/operator heartbeats.

Backs up everything to logs/archive/reset-<timestamp>/ first, then
wipes the live state and restarts the bot + operator. Use when:
  - paper state is full of stale trades from a previous run
  - you want a clean $100k paper capital baseline
  - operator memory needs a hard reset

This is DESTRUCTIVE. It does NOT touch your .env or config.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "logs" / "archive"


def main() -> int:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = ARCHIVE / f"reset-{ts}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fresh-start archive: {archive_dir}")

    # Backup + delete paper state files.
    state_files = [
        "data_cache/paper_state.json",
        "data_cache/trades_state.json",
        "data_cache/heartbeat.json",
        "data_cache/operator.heartbeat",
    ]
    for rel in state_files:
        p = ROOT / rel
        if p.exists():
            target = archive_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            p.unlink()
            print(f"  cleared {rel}")

    # Backup + wipe the memory/ folder entirely.
    mem = ROOT / "memory"
    if mem.exists():
        target = archive_dir / "memory"
        shutil.copytree(mem, target)
        shutil.rmtree(mem)
        print(f"  wiped memory/")

    # Stop any live bot/operator processes.
    print("\nStopping any running bot/operator...")
    for pid_file in [ROOT / "logs" / "bot.pid", ROOT / "logs" / "operator.pid"]:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                import psutil  # type: ignore
                psutil.Process(pid).terminate()
                print(f"  stopped PID={pid}")
            except Exception:
                # Fallback to taskkill on Windows.
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, check=False)
            pid_file.unlink()

    # Run the bot's official reset for any leftover bits.
    print("\nRunning python -m crypto_options_bot reset ...")
    res = subprocess.run(
        [sys.executable, "-m", "crypto_options_bot", "reset"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    print(res.stdout.strip() or "(no output)")
    if res.returncode != 0:
        print(f"  reset returned {res.returncode}: {res.stderr[:200]}")

    print("\nFresh start complete. Ready to launch bot + operator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
