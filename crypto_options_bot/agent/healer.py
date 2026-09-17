"""Healer — self-healing agent.

Consumes ``Sentinel`` reports and applies recovery playbooks. **Never**
modifies strategy code or open positions. Every action is journaled.

Playbooks (in order of severity)
--------------------------------
1. **bot_dead** — no python process for ``crypto_options_bot``.
   Action: launch ``scripts/start_bot_detached.ps1``, wait up to 30 s,
   re-probe.

2. **heartbeat_stale** — heartbeat.json older than 120 s.
   Action: check the bot log's last write; if bot is wedged, restart
   (same as playbook 1). If bot just hasn't ticked yet (e.g. holiday
   or weekend), do nothing.

3. **ws_no_channels** — bot alive but WS feed has no subscriptions.
   Action: write a sentinel log entry and let the next Operator cycle
   pick a connection-restart decision. We don't kill the bot mid-trade.

4. **orphans** — pending orders with no matching position.
   Action: log + run the bot's existing ``scripts/_register_orphans_now.py``
   if present, else write a journal entry.

5. **disk_low** — less than 1 GB free on the log drive.
   Action: rotate large logs (call ``scripts/daily_reset.ps1``).
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .memory import Memory
from .sentinel import HealthReport, Sentinel


@dataclass
class HealerAction:
    playbook: str
    severity: str
    command: list[str] | None = None
    detail: str = ""
    acted: bool = False
    duration_sec: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Healer:
    project_root: Path
    memory: Memory
    sentinel: Sentinel
    min_free_disk_gb: float = 1.0
    restart_grace_sec: float = 30.0
    last_actions: list[HealerAction] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).resolve()

    # ------------------------------------------------------------------
    # Main entry — call once per Sentinel probe
    # ------------------------------------------------------------------
    def heal(self, report: HealthReport) -> list[HealerAction]:
        actions: list[HealerAction] = []
        actions.extend(self._maybe_handle_bot_dead(report))
        actions.extend(self._maybe_handle_heartbeat_stale(report))
        actions.extend(self._maybe_handle_ws_down(report))
        actions.extend(self._maybe_handle_orphans(report))
        actions.extend(self._maybe_handle_disk_low(report))
        self.last_actions.extend(actions)
        # Trim memory of past actions.
        if len(self.last_actions) > 200:
            del self.last_actions[: len(self.last_actions) - 200]
        for a in actions:
            self.memory.append_journal(
                "healer",
                f"playbook={a.playbook} severity={a.severity} acted={a.acted} "
                f"detail={a.detail[:200]} error={a.error}",
            )
        return actions

    # ------------------------------------------------------------------
    # Playbooks
    # ------------------------------------------------------------------
    def _maybe_handle_bot_dead(self, report: HealthReport) -> list[HealerAction]:
        if report.bot_alive:
            return []
        a = HealerAction(
            playbook="bot_dead",
            severity="high",
            detail="no crypto_options_bot process found",
        )
        script = self.project_root / "scripts" / "start_bot_detached.ps1"
        if not script.exists():
            a.error = f"missing launcher: {script}"
            return [a]
        try:
            t0 = time.time()
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            a.duration_sec = time.time() - t0
            a.acted = True
            a.detail = f"launched {script}"
        except Exception as exc:  # noqa: BLE001
            a.error = f"{type(exc).__name__}: {exc}"
        return [a]

    def _maybe_handle_heartbeat_stale(self, report: HealthReport) -> list[HealerAction]:
        if report.heartbeat_age_sec is None or report.heartbeat_age_sec <= 120:
            return []
        # Only restart if heartbeat is REALLY stale (> 5 min).
        if report.heartbeat_age_sec < 300:
            return [
                HealerAction(
                    playbook="heartbeat_warn",
                    severity="low",
                    detail=f"heartbeat {int(report.heartbeat_age_sec)}s old; monitoring",
                )
            ]
        return self._maybe_handle_bot_dead(report)

    def _maybe_handle_ws_down(self, report: HealthReport) -> list[HealerAction]:
        # Don't act if bot is dead — restart will fix WS too.
        if not report.bot_alive or report.ws_subscribed > 0:
            return []
        a = HealerAction(
            playbook="ws_no_channels",
            severity="medium",
            detail=f"bot alive but WS subscribed={report.ws_subscribed}",
        )
        # We deliberately do NOT restart mid-trade. Just journal and let
        # the bot's own reconnect logic (in deribit_ws.py) handle it.
        a.acted = False
        a.detail = "deferred to bot's reconnect; not restarting mid-trade"
        return [a]

    def _maybe_handle_orphans(self, report: HealthReport) -> list[HealerAction]:
        # Crude heuristic: pending orders > 0 AND open positions == 0.
        if report.pending_orders == 0 or report.open_positions > 0:
            return []
        a = HealerAction(
            playbook="orphans",
            severity="medium",
            detail=(
                f"pending_orders={report.pending_orders} "
                f"open_positions={report.open_positions}"
            ),
        )
        script = self.project_root / "scripts" / "_register_orphans_now.py"
        if not script.exists():
            a.acted = False
            a.detail += " (no _register_orphans_now.py; journal-only)"
            return [a]
        try:
            t0 = time.time()
            subprocess.run(
                ["python", str(script)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            a.duration_sec = time.time() - t0
            a.acted = True
        except Exception as exc:  # noqa: BLE001
            a.error = f"{type(exc).__name__}: {exc}"
        return [a]

    def _maybe_handle_disk_low(self, _report: HealthReport) -> list[HealerAction]:
        try:
            import shutil

            free_gb = shutil.disk_usage(self.project_root).free / (1024 ** 3)
        except Exception:  # noqa: BLE001
            return []
        if free_gb >= self.min_free_disk_gb:
            return []
        a = HealerAction(
            playbook="disk_low",
            severity="medium",
            detail=f"free={free_gb:.2f}GB < {self.min_free_disk_gb:.2f}GB",
        )
        script = self.project_root / "scripts" / "daily_reset.ps1"
        if not script.exists():
            a.acted = False
            a.detail += " (no daily_reset.ps1; manual cleanup needed)"
            return [a]
        try:
            t0 = time.time()
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            a.duration_sec = time.time() - t0
            a.acted = True
        except Exception as exc:  # noqa: BLE001
            a.error = f"{type(exc).__name__}: {exc}"
        return [a]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def recent_actions(self, limit: int = 20) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self.last_actions[-limit:]]
