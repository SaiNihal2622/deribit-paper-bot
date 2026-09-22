"""crypto_supervisor.py — Python watchdog for the crypto-options-bot.

Mirrors kotak-neo-bot/scripts/supervisor_daemon.py. Runs as a foreground
process intended to be launched by the CryptoSupervisor scheduled task
(registered by install_crypto_supervisor.py via the force-action JSON trick,
which exploits the SYSTEM-context bot to do the schtasks call).

Why Python instead of PowerShell:
  - Avoids $pid/$PID read-only variable confusion
  - Easier to test
  - Cross-platform if we ever migrate to Linux

Checks every 30s:
  1. NSSM service CryptoOptionsBot — start if not RUNNING
  2. Bot liveness.json freshness — restart if stale (>180s)
  3. Bot PID actually alive — restart if dead
  4. Orphan sweep every 15 min — kill crypto_options_bot zombies

Logs to logs/supervisor_daemon.log.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.resolve()
DCACHE = ROOT / "data_cache"
LOGS = ROOT / "logs"
NSSM_EXE = Path(r"C:\Tools\nssm\nssm-2.24\win64\nssm.exe")
PYTHON_EXE = Path(r"C:\Program Files\Python312\python.exe")
SERVICE_BOT = "CryptoOptionsBot"
LOG_FILE = LOGS / "supervisor_daemon.log"
CHECK_INTERVAL_SEC = 30
STALE_THRESHOLD_SEC = 180
ORPHAN_KILL_EVERY = 30  # every 30 cycles (~15 min)

LOGS.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("supervisor")


def _sc_query(service: str) -> str:
    """Return NSSM service status via sc query."""
    try:
        r = subprocess.run(
            ["sc", "query", service],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return "UNKNOWN"
        out = (r.stdout or "").upper()
        for state in ("RUNNING", "STOPPED", "START_PENDING", "STOP_PENDING"):
            if f"STATE              : 4  {state}" in out or f"STATE              : 1  {state}" in out:
                return state
        return "UNKNOWN"
    except Exception as e:
        log.error(f"sc query {service} failed: {e}")
        return "UNKNOWN"


def _nssm_start(service: str) -> bool:
    try:
        r = subprocess.run(
            [str(NSSM_EXE), "start", service],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0:
            log.info(f"[supervisor] nssm start {service} OK")
            return True
        log.warning(f"[supervisor] nssm start {service} exit={r.returncode}: {(r.stdout or r.stderr or '').strip()[:200]}")
        return False
    except Exception as e:
        log.error(f"nssm start {service} exception: {e}")
        return False


def _nssm_restart(service: str) -> bool:
    try:
        # Clean up orphan zombies FIRST so NSSM restart doesn't wedge on file locks
        _clean_orphans()
        r = subprocess.run(
            [str(NSSM_EXE), "restart", service],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            log.info(f"[supervisor] nssm restart {service} OK")
            return True
        log.warning(f"[supervisor] nssm restart {service} exit={r.returncode}: {(r.stdout or r.stderr or '').strip()[:200]}")
        return False
    except Exception as e:
        log.error(f"nssm restart {service} exception: {e}")
        return False


def _clean_orphans() -> None:
    """Run the orphan-killer as a subprocess so it can use SYSTEM privileges
    to enumerate and kill processes we cannot touch from this scope."""
    try:
        r = subprocess.run(
            [str(PYTHON_EXE), str(ROOT / "scripts" / "crypto_orphan_killer.py"), "--clean"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode in (0, 1):
            log.info(f"[supervisor] orphan sweep exit={r.returncode}")
        else:
            log.warning(f"[supervisor] orphan sweep failed exit={r.returncode}: {(r.stderr or '').strip()[:200]}")
    except Exception as e:
        log.warning(f"orphan sweep exception: {e}")


def _bot_liveness() -> dict:
    liv = DCACHE / "liveness.json"
    if not liv.exists():
        return {"pid": None, "age_sec": 999999, "state": "missing"}
    try:
        d = json.loads(liv.read_text(encoding="utf-8"))
        ts_str = d.get("ts", "")
        if not ts_str:
            return {"pid": d.get("pid"), "age_sec": 999999, "state": "no_ts"}
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return {"pid": d.get("pid"), "age_sec": age, "state": d.get("state", "?")}
    except Exception as e:
        return {"pid": None, "age_sec": 999999, "state": f"unparseable: {e}"}


def _is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
            capture_output=True, text=True, timeout=5,
        )
        return (r.stdout or "").strip() == str(pid)
    except Exception:
        return False


def _fallback_direct_start() -> bool:
    """Last-resort: spawn the bot directly as python (without NSSM wrapper)."""
    try:
        subprocess.Popen(
            [str(PYTHON_EXE), "-u", "-m", "crypto_options_bot", "paper",
             "--feed", "ws", "--recover-orphan", "--dashboard-port", "8511"],
            cwd=str(ROOT),
            creationflags=0x00000008,  # DETACHED_PROCESS
        )
        log.warning("[supervisor] direct python start (NSSM bypass)")
        return True
    except Exception as e:
        log.error(f"direct start failed: {e}")
        return False


def run_cycle(cycle: int) -> None:
    log.info(f"[supervisor] cycle={cycle} start")
    bot_nssm = _sc_query(SERVICE_BOT)
    log.info(f"[supervisor] cycle={cycle} {SERVICE_BOT} nssm={bot_nssm}")
    if bot_nssm != "RUNNING":
        log.warning(f"[supervisor] cycle={cycle} {SERVICE_BOT} NSSM is {bot_nssm} - starting")
        if not _nssm_start(SERVICE_BOT):
            log.warning(f"[supervisor] cycle={cycle} NSSM start failed, falling back to direct start")
            _fallback_direct_start()
        return

    time.sleep(5)  # let the bot write liveness after a (re)start
    liv = _bot_liveness()
    if liv["age_sec"] > STALE_THRESHOLD_SEC:
        log.warning(f"[supervisor] cycle={cycle} liveness stale (age={int(liv['age_sec'])}s, pid={liv['pid']}) - restarting")
        if _nssm_restart(SERVICE_BOT):
            log.info("[supervisor] bot restarted via NSSM")
        else:
            log.warning("[supervisor] NSSM restart failed; direct start fallback")
            _fallback_direct_start()
        return

    if liv["pid"] and not _is_alive(liv["pid"]):
        log.warning(f"[supervisor] cycle={cycle} PID {liv['pid']} not running but liveness says {liv['state']} - restarting")
        _nssm_restart(SERVICE_BOT)

    if cycle % ORPHAN_KILL_EVERY == 0:
        _clean_orphans()

    if cycle % 20 == 0:
        log.info(
            f"[supervisor] cycle={cycle} OK | nssm={bot_nssm} | "
            f"pid={liv['pid']} | liveness_age={int(liv['age_sec'])}s"
        )


def main() -> int:
    log.info("=" * 50)
    log.info(f"[supervisor] starting (interval={CHECK_INTERVAL_SEC}s, stale={STALE_THRESHOLD_SEC}s)")
    try:
        ctx = os.environ.get("USERNAME", "?")
        sid = os.environ.get("SESSIONNAME", "?")
        log.info(f"[supervisor] context: USERNAME={ctx} SESSIONNAME={sid}")
    except Exception:
        pass
    cycle = 0
    while True:
        cycle += 1
        try:
            run_cycle(cycle)
        except Exception as e:
            log.error(f"[supervisor] cycle={cycle} exception: {e}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
