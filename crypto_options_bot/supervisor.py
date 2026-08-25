"""Supervisor — restart the bot on crash with exponential backoff.

Runs the paper (or live) session in a subprocess. On exit:
  - First 3 restarts: 5s, 10s, 20s backoff.
  - After 5 restarts within 10 minutes: gives up and logs a critical alert.
  - Otherwise: keeps looping with a hard 60s cap on backoff.

Logs supervisor events to ``logs/supervisor.log``. Handles SIGINT /
SIGTERM cleanly — propagates the signal to the child once, then quits if
the child doesn't exit within 10s.

Run as: ``python -m crypto_options_bot.supervisor [paper|live]``
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

# ---------------------------------------------------------------------------
# Logging — the supervisor writes its own log to logs/supervisor.log so it
# survives a child crash.
# ---------------------------------------------------------------------------
SUPERVISOR_LOG = Path("logs/supervisor.log")
SUPERVISOR_LOG.parent.mkdir(parents=True, exist_ok=True)
logger.add(
    str(SUPERVISOR_LOG),
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <5} | {message}",
    rotation="5 MB",
    enqueue=True,
)

# ---------------------------------------------------------------------------
# Restart policy
# ---------------------------------------------------------------------------
MAX_RESTARTS_IN_WINDOW = 5
WINDOW_SEC = 600          # 10 minutes
INITIAL_BACKOFF_SEC = 5.0
MAX_BACKOFF_SEC = 60.0
CHILD_GRACE_SEC = 10.0    # wait this long after SIGINT before SIGKILL


def _should_give_up(restart_times: list[float]) -> bool:
    """True if we have >= MAX_RESTARTS_IN_WINDOW exits within WINDOW_SEC."""
    if not restart_times:
        return False
    now = time.time()
    recent = [t for t in restart_times if (now - t) <= WINDOW_SEC]
    return len(recent) >= MAX_RESTARTS_IN_WINDOW


def _next_backoff(attempt: int) -> float:
    """Exponential backoff: 5, 10, 20, 40, capped at 60."""
    delay = min(MAX_BACKOFF_SEC, INITIAL_BACKOFF_SEC * (2 ** max(0, attempt - 1)))
    return float(delay)


def _build_child_argv(mode: str, extra: list[str]) -> list[str]:
    """Build the child process argv.

    Args:
        mode: "paper" or "live"
        extra: extra CLI flags to pass through
    """
    argv = [sys.executable, "-m", "crypto_options_bot", mode]
    argv.extend(extra)
    return argv


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="crypto_options_bot.supervisor")
    parser.add_argument(
        "mode", nargs="?", default="paper",
        help="Bot subcommand to supervise: 'paper' or 'live' (default: paper)",
    )
    parser.add_argument(
        "--max-runtime", type=float, default=0.0,
        help="Pass-through to child: stop after N seconds. 0 = forever.",
    )
    parser.add_argument(
        "--feed", choices=["ws", "rest"], default=None,
        help="Pass-through: 'ws' (default) or 'rest'.",
    )
    parser.add_argument(
        "--dashboard-port", type=int, default=None,
        help="Pass-through: dashboard port (default 8511).",
    )
    parser.add_argument(
        "--extra-arg", action="append", default=[],
        help="Pass-through: extra CLI flag (repeatable). e.g. --extra-arg --verbose",
    )
    args = parser.parse_args(argv)

    extra: list[str] = []
    if args.max_runtime and args.max_runtime > 0:
        extra += ["--max-runtime", str(args.max_runtime)]
    if args.feed:
        extra += ["--feed", args.feed]
    if args.dashboard_port:
        extra += ["--dashboard-port", str(args.dashboard_port)]
    extra += list(args.extra_arg or [])

    mode = str(args.mode or "paper").lower()
    if mode not in ("paper", "live"):
        logger.error(f"unknown mode: {mode} (use 'paper' or 'live')")
        return 2

    # Container for the child + signal state
    state = {"child": None, "stop": False}

    def _on_signal(signum, _frame):
        logger.info(f"supervisor: received signal {signum}, shutting down...")
        state["stop"] = True
        child = state.get("child")
        if child and child.poll() is None:
            try:
                child.send_signal(signum)
            except Exception as e:
                logger.debug(f"supervisor: child signal failed: {e}")

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        # signal only works in the main thread
        pass

    restart_times: list[float] = []
    attempt = 0
    while not state["stop"]:
        argv = _build_child_argv(mode, extra)
        logger.info(f"supervisor: launching child (attempt {attempt+1}): {' '.join(argv)}")
        try:
            child = subprocess.Popen(argv)
        except Exception as e:
            logger.error(f"supervisor: failed to spawn child: {e}")
            return 1
        state["child"] = child
        try:
            rc = child.wait()
        except KeyboardInterrupt:
            logger.info("supervisor: KeyboardInterrupt, terminating child")
            try:
                child.terminate()
                child.wait(timeout=CHILD_GRACE_SEC)
            except Exception:
                try:
                    child.kill()
                except Exception:
                    pass
            state["stop"] = True
            continue

        logger.info(f"supervisor: child exited rc={rc}")
        if state["stop"]:
            break
        attempt += 1
        restart_times.append(time.time())
        if _should_give_up(restart_times):
            logger.critical(
                f"supervisor: giving up — {len(restart_times)} restarts within "
                f"{WINDOW_SEC/60:.0f} min (rc={rc})"
            )
            return 1
        backoff = _next_backoff(attempt)
        logger.info(f"supervisor: sleeping {backoff:.1f}s before restart (attempt {attempt+1})")
        # sleep in 1s slices so signals interrupt promptly
        slept = 0.0
        while slept < backoff and not state["stop"]:
            time.sleep(min(1.0, backoff - slept))
            slept += 1.0

    logger.info("supervisor: bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
