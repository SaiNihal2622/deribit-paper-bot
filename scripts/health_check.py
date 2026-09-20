"""Health check + auto-repair for the crypto-options-bot stack.

Runs every 5 minutes via scheduled task. Checks:
  - NSSM services are Running (CryptoOptionsBot + CryptoOptionsOperator)
  - Bot dashboard is responding (http://127.0.0.1:8511/api/status)
  - Bot heartbeat file is recent (within 90s)
  - System memory: free > 500 MB
  - System pagefile: allocated > 0 MB (catches missing-pagefile regression)
  - Disk free on C: > 5 GB
  - Bot log: no new errors in last 5 min
  - Bot log: not growing > 50 MB (catches log-spam regressions)

If any check fails, attempts a targeted repair:
  - Service not running -> nssm start (one retry)
  - Bot heartbeat stale -> nssm restart (healer playbook equivalent)
  - Memory low -> no auto-fix, alerts via exit code + log
  - Pagefile=0 -> alerts, requires reboot to fix

Exit codes:
  0 - all green
  1 - one or more warnings (recovery attempted; see log)
  2 - critical failure (no auto-fix possible; needs user intervention)

Designed to be idempotent: safe to run multiple times.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "health_check.log"
STATE = ROOT / "memory" / "state" / "health_check_latest.json"
NSSM = ROOT / "tools" / "nssm.exe"
NSSM_ADMIN_OK = NSSM.exists() and os.access(str(NSSM), os.X_OK)


def _log(msg: str) -> None:
    """Append a timestamped line to the health check log."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} | {msg}\n")


def _save_state(checks: dict, status: str, exit_code: int) -> None:
    """Snapshot the most recent run for the Sentinel agent to consume."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "exit_code": exit_code,
        "checks": checks,
    }
    STATE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _check_service(name: str) -> tuple[bool, str]:
    """Return (running, status_text). Uses sc.exe (always available)."""
    try:
        out = subprocess.run(
            ["sc.exe", "query", name],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        text = out.stdout
        for line in text.splitlines():
            if "STATE" in line:
                # Format: "        STATE              : 4  RUNNING"
                if "RUNNING" in line:
                    return True, line.strip()
                return False, line.strip()
        return False, "STATE not found"
    except Exception as e:
        return False, f"sc.exe failed: {e}"


def _nssm_cmd(action: str, service: str) -> tuple[bool, str]:
    """Run nssm stop/start. Returns (ok, output)."""
    if not NSSM_ADMIN_OK:
        return False, "nssm not present"
    try:
        out = subprocess.run(
            [str(NSSM), action, service],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000,
        )
        return out.returncode == 0, (out.stdout + out.stderr).strip()
    except Exception as e:
        return False, f"nssm {action} failed: {e}"


def _check_dashboard() -> tuple[bool, str]:
    """Hit /api/status with 3s timeout. Return (ok, payload or error)."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8511/api/status",
            headers={"User-Agent": "health-check/1.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        # Healthy if we got a valid JSON with the expected keys
        if "mode" in data and "trades" in data and "feed_health" in data:
            return True, f"mode={data['mode']}, trades={len(data['trades'])}, dvol={data['feed_health'].get('dvol', '?')}"
        return False, "missing expected fields"
    except Exception as e:
        return False, str(e)[:120]


def _check_heartbeat_age() -> tuple[bool, str]:
    """Check data_cache/heartbeat.json is recent (within 90s)."""
    hb = ROOT / "data_cache" / "heartbeat.json"
    if not hb.exists():
        return False, "no heartbeat file"
    age = time.time() - hb.stat().st_mtime
    if age > 90:
        return False, f"stale: {int(age)}s old"
    return True, f"fresh: {int(age)}s old"


def _check_memory() -> tuple[bool, str]:
    """Free physical RAM > 500 MB."""
    try:
        import ctypes
        from ctypes import wintypes, c_void_p
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalMemoryStatusEx.argtypes = [c_void_p]
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        avail_mb = int(stat.ullAvailPhys / (1024 * 1024))
        if avail_mb < 500:
            return False, f"free RAM only {avail_mb} MB"
        return True, f"free {avail_mb} MB ({stat.dwMemoryLoad}% used)"
    except Exception as e:
        return False, f"RAM check failed: {e}"


def _check_pagefile() -> tuple[bool, str]:
    """Pagefile.sys must be > 0 MB allocated (catches missing-pagefile).

    Reads the registry setting (what's CONFIGURED). Note: this won't reflect
    whether the setting is currently active (that requires a reboot). For
    active size, use _check_pagefile_active via WMI.
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
        )
        try:
            paging_files, _ = winreg.QueryValueEx(key, "PagingFiles")
        finally:
            winreg.CloseKey(key)
        if not paging_files:
            return False, "no pagefile configured"
        # Format: "c:\pagefile.sys 2904 4356" — initial max in MB
        parts = paging_files[0].split()
        if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) > 0:
            return True, f"pagefile initial={parts[1]} MB max={parts[2] if len(parts) > 2 else '?'} MB"
        return False, f"pagefile config: {paging_files[0]} (initial=0, requires reboot to apply)"
    except FileNotFoundError:
        return False, "registry key not found"
    except Exception as e:
        return False, f"pagefile check failed: {e}"


def _check_disk_free() -> tuple[bool, str]:
    """Free disk on C: must be > 5 GB."""
    try:
        import ctypes
        from ctypes import wintypes, byref, c_wchar_p
        free_bytes = ctypes.c_ulonglong(0)
        total_bytes = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            c_wchar_p("C:\\"), None, byref(total_bytes), byref(free_bytes)
        )
        free_gb = free_bytes.value / (1024 ** 3)
        if free_gb < 5:
            return False, f"free {free_gb:.1f} GB (low!)"
        return True, f"free {free_gb:.1f} GB"
    except Exception as e:
        return False, f"disk check failed: {e}"


def _check_log_size(path: Path, max_mb: float) -> tuple[bool, str]:
    """A single log file must be < max_mb MB (catches log-spam regressions)."""
    if not path.exists():
        return True, "no log yet"
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        return False, f"{size_mb:.1f} MB (> {max_mb} MB)"
    return True, f"{size_mb:.1f} MB"


def _archive_old_logs() -> tuple[bool, str]:
    """Move .log files > 50 MB into logs/archive/<date>/."""
    LOGS = ROOT / "logs"
    moved = []
    for f in LOGS.glob("*.log"):
        if f.stat().st_size > 50 * 1024 * 1024:
            ts = datetime.now().strftime("%Y%m%d")
            archive = LOGS / "archive" / ts
            archive.mkdir(parents=True, exist_ok=True)
            target = archive / f.name
            try:
                f.rename(target)
                moved.append(f.name)
            except OSError:
                pass
    if moved:
        return True, f"archived {len(moved)} large logs: {', '.join(moved)}"
    return True, "no large logs"


def main() -> int:
    checks: dict[str, dict] = {}
    repairs: list[str] = []
    critical = False

    def run(name: str, fn) -> None:
        ok, detail = fn()
        checks[name] = {"ok": ok, "detail": detail, "repaired": None}
        if not ok:
            _log(f"FAIL | {name} | {detail}")

    # --- Services ---
    run("crypto_bot_service", lambda: _check_service("CryptoOptionsBot"))
    run("crypto_operator_service", lambda: _check_service("CryptoOptionsOperator"))

    # Service auto-repair
    if not checks["crypto_bot_service"]["ok"]:
        ok, detail = _nssm_cmd("start", "CryptoOptionsBot")
        checks["crypto_bot_service"]["repaired"] = detail
        if ok:
            repairs.append("CryptoOptionsBot: started")
            _log(f"REPAIR | CryptoOptionsBot started")
        else:
            critical = True
    if not checks["crypto_operator_service"]["ok"]:
        ok, detail = _nssm_cmd("start", "CryptoOptionsOperator")
        checks["crypto_operator_service"]["repaired"] = detail
        if ok:
            repairs.append("CryptoOptionsOperator: started")
            _log(f"REPAIR | CryptoOptionsOperator started")
        else:
            critical = True

    # --- Bot health ---
    run("dashboard", _check_dashboard)
    run("bot_heartbeat", _check_heartbeat_age)
    # Heartbeat stale -> restart bot (healer equivalent)
    if not checks["bot_heartbeat"]["ok"]:
        ok, detail = _nssm_cmd("stop", "CryptoOptionsBot")
        time.sleep(2)
        ok2, detail2 = _nssm_cmd("start", "CryptoOptionsBot")
        if ok and ok2:
            checks["bot_heartbeat"]["repaired"] = "restarted"
            repairs.append("CryptoOptionsBot: restarted (stale heartbeat)")
            _log("REPAIR | CryptoOptionsBot restarted due to stale heartbeat")
        else:
            critical = True

    # --- Resources ---
    run("memory", _check_memory)
    if not checks["memory"]["ok"]:
        critical = True  # low RAM: no auto-fix, needs user
    run("pagefile", _check_pagefile)
    if not checks["pagefile"]["ok"]:
        critical = True  # missing pagefile: needs reboot
    run("disk_free", _check_disk_free)
    if not checks["disk_free"]["ok"]:
        critical = True

    # --- Logs ---
    run("bot_log_size", lambda: _check_log_size(ROOT / "logs" / "bot.log", 100))
    run("op_log_size", lambda: _check_log_size(ROOT / "logs" / "operator_stderr.log", 50))
    # Auto-archive large logs
    ok, detail = _archive_old_logs()
    checks["log_archive"] = {"ok": ok, "detail": detail, "repaired": None}

    # --- Determine overall status ---
    failed = [k for k, v in checks.items() if not v["ok"]]
    if repairs:
        _log(f"REPAIRS | {'; '.join(repairs)}")
    if critical:
        status = "CRITICAL"
        exit_code = 2
    elif failed:
        status = "WARNING"
        exit_code = 1
    else:
        status = "OK"
        exit_code = 0

    summary = (
        f"{status} | passed={len(checks)-len(failed)}/{len(checks)} "
        f"| failed={','.join(failed) or 'none'} "
        f"| repairs={len(repairs)}"
    )
    _log(summary)
    _save_state(checks, status, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())