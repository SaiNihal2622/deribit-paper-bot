"""Daily maintenance: archive old logs, clear temp, prune memory dir.

Runs once per day via scheduled task. Idempotent.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "maintenance.log"
STATE = ROOT / "memory" / "state" / "maintenance_latest.json"


def _log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} | {msg}\n")


def _archive_old_logs(retention_days: int = 7) -> int:
    """Move .log files older than retention_days into logs/archive/."""
    LOGS = ROOT / "logs"
    moved = 0
    cutoff = time.time() - retention_days * 86400
    for f in LOGS.glob("*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                ts = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y%m%d")
                archive = LOGS / "archive" / ts
                archive.mkdir(parents=True, exist_ok=True)
                target = archive / f.name
                if not target.exists():
                    f.rename(target)
                    moved += 1
        except OSError:
            pass
    # Also archive any old .jsonl in memory/health (older than 30 days)
    health = ROOT / "memory" / "health"
    if health.exists():
        cutoff_30 = time.time() - 30 * 86400
        for f in health.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff_30:
                    ts = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y%m%d")
                    archive = health / "archive" / ts
                    archive.mkdir(parents=True, exist_ok=True)
                    target = archive / f.name
                    if not target.exists():
                        f.rename(target)
                        moved += 1
            except OSError:
                pass
    return moved


def _prune_operator_memory() -> int:
    """Keep memory/proposals/ to last 10 entries, memory/lessons/ to last 30."""
    moved = 0
    for subdir, keep in [("proposals", 10), ("lessons", 30)]:
        d = ROOT / "memory" / subdir
        if not d.exists():
            continue
        files = sorted(d.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            try:
                target = d / "archive" / f.name
                target.parent.mkdir(parents=True, exist_ok=True)
                f.rename(target)
                moved += 1
            except OSError:
                pass
    return moved


def _clear_pycache() -> int:
    """Remove __pycache__ directories older than 1 day."""
    removed = 0
    cutoff = time.time() - 86400
    for d in ROOT.rglob("__pycache__"):
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


def _save_state(action: str, count: int) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "items_moved": count,
    }
    STATE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    _log("=== daily maintenance starting ===")
    moved_logs = _archive_old_logs(retention_days=7)
    _log(f"archived {moved_logs} old logs")
    pruned = _prune_operator_memory()
    _log(f"pruned {pruned} old memory artifacts")
    cleared = _clear_pycache()
    _log(f"cleared {cleared} stale __pycache__ dirs")
    total = moved_logs + pruned + cleared
    _log(f"=== daily maintenance done ({total} items moved) ===")
    _save_state("daily", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())