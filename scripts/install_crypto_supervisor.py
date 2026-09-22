"""install_crypto_supervisor.py — register the SYSTEM-context supervisor as a scheduled task.

Mirrors kotak-neo-bot/scripts/install_supervisor_task.py. The trick: a
user-context session cannot install SYSTEM scheduled tasks (no UAC
elevation in a non-interactive shell). But the bot, when run as SYSTEM via
NSSM, CAN call `schtasks /create /RU SYSTEM /RL HIGHEST`. So we write a
"force-action JSON" describing the schtasks command; the SYSTEM bot reads
this file and runs the command on our behalf.

Usage:
  python scripts/install_crypto_supervisor.py            # register task
  python scripts/install_crypto_supervisor.py --start    # also fire it now
  python scripts/install_crypto_supervisor.py --remove   # unregister
  python scripts/install_crypto_supervisor.py --status   # query

The JSON file is consumed by a small handler in __main__.py that polls
data_cache/mavis_force_action.json on each cycle. Once consumed, the file
is renamed to .consumed-<timestamp> so it doesn't fire twice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.resolve()
DCACHE = ROOT / "data_cache"
SUPERVISOR_PS1 = ROOT / "system" / "crypto_supervisor_loop.ps1"
FORCE_ACTION = DCACHE / "mavis_force_action.json"

PS_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

TASK_NAME = "CryptoSupervisor"


def _build_register_action(also_start: bool) -> dict:
    """Build the JSON that registers the SYSTEM supervisor task."""
    cmd_str = (
        f'"{PS_EXE}" -NoProfile -ExecutionPolicy Bypass -File "{SUPERVISOR_PS1}"'
    )
    schtasks_cmd = [
        "schtasks", "/create",
        "/tn", TASK_NAME,
        "/tr", cmd_str,
        "/sc", "ONSTART",
        "/ru", "SYSTEM",
        "/rl", "HIGHEST",
        "/f",
    ]
    out = {
        "action": "RUN_COMMAND",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "consumed": False,
        "command": schtasks_cmd,
        "reason": "register CryptoSupervisor (SYSTEM-context 24/7 watchdog)",
        "timeout": 30,
    }
    if also_start:
        out["command_after_register"] = ["schtasks", "/run", "/tn", TASK_NAME]
    return out


def _build_remove_action() -> dict:
    return {
        "action": "RUN_COMMAND",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "consumed": False,
        "command": ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        "reason": "remove CryptoSupervisor scheduled task",
        "timeout": 15,
    }


def _build_start_action() -> dict:
    return {
        "action": "RUN_COMMAND",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "consumed": False,
        "command": ["schtasks", "/run", "/tn", TASK_NAME],
        "reason": "start CryptoSupervisor task now",
        "timeout": 15,
    }


def _status() -> int:
    """Print whether the task is registered and last status."""
    import subprocess
    r = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "list"],
        capture_output=True, text=True, timeout=10,
    )
    print(r.stdout or "")
    if r.returncode != 0:
        print(f"[warn] schtasks /query exit={r.returncode}: {(r.stderr or '').strip()}")
    return 0 if r.returncode == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true", help="Also start the task immediately")
    parser.add_argument("--remove", action="store_true", help="Remove the supervisor task")
    parser.add_argument("--status", action="store_true", help="Query task status")
    args = parser.parse_args()

    if args.status:
        return _status()

    if args.remove:
        action = _build_remove_action()
        print(f"Writing REMOVE action to {FORCE_ACTION}")
    else:
        action = _build_register_action(also_start=args.start)
        print(f"Writing REGISTER action to {FORCE_ACTION}")
        if not SUPERVISOR_PS1.exists():
            print(f"ERROR: supervisor_loop.ps1 not found at {SUPERVISOR_PS1}")
            return 1

    DCACHE.mkdir(parents=True, exist_ok=True)
    FORCE_ACTION.write_text(json.dumps(action, indent=2), encoding="utf-8")
    print(f"The bot (running as SYSTEM via NSSM) will execute this within ~30s.")
    print(f"After execution, verify with: schtasks /query /tn {TASK_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
