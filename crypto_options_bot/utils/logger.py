"""Loguru-based logger setup.

Centralised so every module does `from loguru import logger` and gets the
configured sink (console + optional file). Mirrors the kotak-neo-bot style.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

from loguru import logger


_configured = False


def setup_logger(level: str = "INFO", log_file: str = "logs/bot.log") -> None:
    """Configure loguru sinks (idempotent).

    Args:
        level: log level (DEBUG/INFO/WARNING/ERROR)
        log_file: path to rotating log file. Parent dirs are created.
    """
    global _configured
    if _configured:
        return
    _configured = True

    logger.remove()
    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}:{function}:{line}</cyan> | "
        "<level>{message}</level>"
    )
    logger.add(sys.stderr, level=level, format=fmt, colorize=True)
    if log_file:
        try:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logger.add(
                str(log_path),
                level=level,
                format=fmt,
                rotation="20 MB",
                retention="7 days",
                enqueue=True,
            )
        except Exception as e:
            # Logging must never crash the app — fall back to console-only.
            print(f"[logger] file sink init failed: {e}", file=sys.stderr)


def get_logger():
    """Return the configured loguru logger (alias)."""
    return logger
