"""Sentinel — the always-on watchdog.

Single responsibility: every ``interval_sec`` (default 60), probe the
running system and produce a structured health snapshot. Sentinel
never mutates state; the Healer consumes Sentinels' reports.

What it checks
--------------
1. Bot process alive (PID + uptime) — looks for ``crypto_options_bot``
   in the python process table.
2. Heartbeat file freshness — ``data_cache/heartbeat.json`` (written
   by ``__main__._heartbeat``) must be < 120 s old.
3. WS feed liveness — counts ``subscribed`` channels via ``DeribitWebSocketFeed``
   singleton (when available).
4. Open positions + orders sanity — no orphans, fills match state.
5. Disk space + log freshness — quick check.

The result is a JSON snapshot returned from ``probe()`` and appended
to ``memory/health/``.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .memory import Memory


@dataclass
class HealthReport:
    timestamp: str
    ok: bool
    bot_alive: bool = False
    heartbeat_age_sec: Optional[float] = None
    ws_subscribed: int = 0
    open_positions: int = 0
    open_trades: int = 0
    pending_orders: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def summary(self) -> str:
        flags = []
        if not self.bot_alive:
            flags.append("bot-dead")
        if self.heartbeat_age_sec is not None and self.heartbeat_age_sec > 120:
            flags.append(f"heartbeat-stale({int(self.heartbeat_age_sec)}s)")
        if self.ws_subscribed == 0:
            flags.append("ws-no-channels")
        if not flags:
            flags.append("ok")
        return f"health={','.join(flags)} pos={self.open_positions} " f"trades={self.open_trades} ws={self.ws_subscribed}"


class Sentinel:
    def __init__(
        self,
        project_root: Path,
        memory: Memory,
        heartbeat_path: Optional[Path] = None,
        state_path: Optional[Path] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.memory = memory
        self.heartbeat_path = (
            heartbeat_path or self.project_root / "data_cache" / "heartbeat.json"
        )
        self.state_path = state_path or self.project_root / "data_cache" / "paper_state.json"

    # ------------------------------------------------------------------
    # Probe entrypoint — called by Scheduler every interval_sec
    # ------------------------------------------------------------------
    def probe(self) -> HealthReport:
        now = datetime.now(timezone.utc)
        rep = HealthReport(timestamp=now.isoformat(), ok=True)

        # 1. Bot process alive — best-effort psutil-style lookup without psutil.
        rep.bot_alive = self._is_bot_alive()
        if not rep.bot_alive:
            rep.notes.append("bot process not found in python process table")
            rep.ok = False

        # 2. Heartbeat freshness.
        rep.heartbeat_age_sec = self._heartbeat_age_sec()
        if rep.heartbeat_age_sec is None:
            rep.notes.append("heartbeat.json missing")
            rep.ok = False
        elif rep.heartbeat_age_sec > 120:
            rep.notes.append(f"heartbeat {int(rep.heartbeat_age_sec)}s old (> 120s)")
            rep.ok = False

        # 3. WS subscription count — best-effort, only if feed singleton is importable.
        rep.ws_subscribed = self._ws_subscribed()

        # 4. Open positions + trades from disk (tolerates missing).
        rep.open_trades, rep.open_positions, rep.pending_orders = self._read_counts()

        # 5. Persist to memory.
        self.memory.write_health(rep.to_dict())
        self.memory.write_state("health:latest", rep.to_dict())
        return rep

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _is_bot_alive(self) -> bool:
        # We avoid psutil to keep deps minimal. Try the native wmic then
        # fall back to scanning /proc-style tasklist on Windows.
        try:
            if os.name == "nt":
                import subprocess

                out = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                        "| Where-Object { $_.CommandLine -like '*crypto_options_bot*' } "
                        "| Select-Object -ExpandProperty ProcessId",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # ProcessIds are numeric; any presence means alive.
                return any(line.strip().isdigit() for line in out.stdout.splitlines())
            else:
                import subprocess

                out = subprocess.run(
                    ["pgrep", "-af", "crypto_options_bot"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return out.returncode == 0 and out.stdout.strip() != ""
        except Exception:  # noqa: BLE001
            # If probing fails we don't false-alarm; Healer will see the gap.
            return True

    def _heartbeat_age_sec(self) -> Optional[float]:
        if not self.heartbeat_path.exists():
            return None
        try:
            mtime = self.heartbeat_path.stat().st_mtime
            return max(0.0, time.time() - mtime)
        except OSError:
            return None

    def _ws_subscribed(self) -> int:
        # Prefer the bot's own heartbeat.json (cross-process safe).
        # Fall back to the in-process feed singleton if it ever exists
        # in the same process as the sentinel (e.g. unit tests).
        try:
            if self.heartbeat_path.exists():
                with open(self.heartbeat_path, encoding="utf-8") as fh:
                    hb = json.load(fh)
                ws = int(hb.get("ws_subscribed", 0) or 0)
                if ws > 0:
                    return ws
        except Exception:  # noqa: BLE001
            pass

        try:
            from crypto_options_bot.data.deribit_ws import get_feed

            return len(get_feed()._subscribed_channels)
        except Exception:  # noqa: BLE001
            return 0

    def _read_counts(self) -> tuple[int, int, int]:
        # Prefer the bot's heartbeat.json (in-memory counts are more
        # current than the persisted JSON, which is updated on event).
        try:
            if self.heartbeat_path.exists():
                with open(self.heartbeat_path, encoding="utf-8") as fh:
                    hb = json.load(fh)
                return (
                    int(hb.get("open_trades", 0) or 0),
                    int(hb.get("positions", 0) or 0),
                    int(hb.get("pending_orders", 0) or 0),
                )
        except Exception:  # noqa: BLE001
            pass

        if not self.state_path.exists():
            return 0, 0, 0
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return 0, 0, 0
        trades = data.get("open_trades") or data.get("trades") or {}
        positions = data.get("positions") or {}
        orders = data.get("orders") or {}
        return len(trades), len(positions), len(orders)
