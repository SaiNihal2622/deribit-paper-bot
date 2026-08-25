"""Telegram alerter — optional, disabled if env vars missing.

Patterned after the Kotak Neo bot's alerter but trimmed to the essentials
(just enough to surface trade opens/closes and target/stop hits). Uses
urllib for HTTP (no ``requests`` dependency). Sends are dispatched to a
daemon worker thread so the main loop never blocks on Telegram.

Required env vars:
  - TELEGRAM_BOT_TOKEN
  - TELEGRAM_CHAT_ID

If either is missing the alerter is a no-op (sends are silently dropped
after one startup warning). Callers don't need to special-case disabled.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from loguru import logger


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class TelegramAlerter:
    """Optional Telegram notifier.

    Set ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` in the env to enable.
    Without those, ``enabled`` is False and every ``send`` is silently
    dropped (after one log line at construction time).
    """

    API_BASE = "https://api.telegram.org"

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        queue_max: int = 1000,
        timeout: int = 10,
    ):
        self.bot_token = bot_token if bot_token is not None else _env("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id is not None else _env("TELEGRAM_CHAT_ID")
        if enabled is None:
            enabled = bool(self.bot_token and self.chat_id)
        self.enabled = bool(enabled)
        self.timeout = int(timeout)
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if self.enabled:
            self._thread = threading.Thread(
                target=self._worker, name="telegram-alerter", daemon=True
            )
            self._thread.start()
            logger.info("TelegramAlerter ENABLED (chat_id set)")
        else:
            logger.warning(
                "TelegramAlerter disabled: set TELEGRAM_BOT_TOKEN + "
                "TELEGRAM_CHAT_ID in env to enable"
            )

    # ------------------------------------------------------------------
    def send(self, message: str) -> None:
        """Enqueue a message. Drops silently if disabled or queue full."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            logger.debug("telegram queue full, dropping message")

    def notify_trade_opened(self, plan, fills) -> None:
        """Compose + send a 'trade opened' notification.

        Args:
            plan: TradePlan
            fills: list of Order objects (or anything with .order_id, .symbol, .avg_fill_price, .side, .filled_qty)
        """
        if not self.enabled or plan is None:
            return
        try:
            legs = []
            for o in (fills or []):
                fill_price = getattr(o, "avg_fill_price", None) or getattr(o, "expected_fill_price", 0) or 0
                qty = getattr(o, "filled_qty", 0) or 0
                sym = getattr(o, "symbol", "?")
                side = getattr(getattr(o, "side", None), "value", "?")
                legs.append(f"  {side} {qty}x {sym} @ {fill_price:.4f}")
            legs_str = "\n".join(legs) if legs else "  (no legs)"
            msg = (
                f"OPEN: {plan.strategy.value} {plan.underlying}\n"
                f"target={plan.target:.4f} stop={plan.stop:.4f} "
                f"confidence={plan.confidence:.2f}\n"
                f"{legs_str}"
            )
            self.send(msg)
        except Exception as e:
            logger.debug(f"notify_trade_opened error: {e}")

    def notify_trade_closed(self, trade) -> None:
        if not self.enabled or trade is None:
            return
        try:
            pnl = float(getattr(trade, "realized_pnl", 0.0) or 0.0)
            exit_reason = getattr(trade, "exit_reason", "?")
            sign = "+" if pnl >= 0 else ""
            plan = getattr(trade, "plan", None)
            strategy = plan.strategy.value if plan else "?"
            underlying = plan.underlying if plan else "?"
            msg = (
                f"CLOSE: {strategy} {underlying} "
                f"P&L={sign}{pnl:.4f} reason={exit_reason}"
            )
            self.send(msg)
        except Exception as e:
            logger.debug(f"notify_trade_closed error: {e}")

    def notify_target_stop(self, trade, reason: str) -> None:
        """Convenience for target/stop hits."""
        if not self.enabled or trade is None:
            return
        plan = getattr(trade, "plan", None)
        strategy = plan.strategy.value if plan else "?"
        underlying = plan.underlying if plan else "?"
        msg = f"{reason.upper()}: {strategy} {underlying} (trade {getattr(trade, 'trade_id', '?')})"
        self.send(msg)

    # ------------------------------------------------------------------
    def _worker(self) -> None:
        """Drain the queue, sending each message via Telegram Bot API."""
        while not self._stop.is_set():
            try:
                msg = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._post_message(msg)
            except Exception as e:
                logger.debug(f"telegram send failed: {e}")

    def _post_message(self, text: str) -> None:
        url = f"{self.API_BASE}/bot{self.bot_token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status != 200:
                    logger.debug(f"telegram non-200: {r.status}")
        except urllib.error.HTTPError as e:
            logger.debug(f"telegram HTTPError {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            logger.debug(f"telegram URLError: {e.reason}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        logger.info("TelegramAlerter stopped")


__all__ = ["TelegramAlerter"]
