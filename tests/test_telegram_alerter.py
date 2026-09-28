"""test_telegram_alerter.py — verify TelegramAlerter notification methods.

Tests cover the new hooks:
  - notify_regime_gate (BLOCKED + OPEN transitions)
  - notify_expiry_auto_close
  - notify_daily_summary
  - All are no-ops when disabled (no env vars)
  - All are no-ops when queue full (no crash)

We mock the bot token / chat_id so the alerter thinks it's enabled, then
patch the HTTP post so tests never hit the real Telegram API.
"""
from __future__ import annotations

import queue
import unittest
from unittest import mock

from crypto_options_bot.alerts.telegram import TelegramAlerter


def _make_alerter(monkeypatch=None):
    """Build an alerter that's 'enabled' but with HTTP mocked.

    The alerter's worker thread drains the queue asynchronously, so we
    snapshot queue contents synchronously after `send()` to assert what
    would be sent without racing the worker.
    """
    alerter = TelegramAlerter(
        bot_token="test-token",
        chat_id="12345",
        enabled=True,
        queue_max=10,
    )
    # Patch the post method AFTER construction. The worker thread looks
    # up self._post_message at call time, so this rebinds correctly.
    alerter._post_message = mock.MagicMock()
    return alerter


def _drain_queue(alerter) -> list[str]:
    """Pull all currently-queued messages synchronously (without waiting
    for the worker thread)."""
    msgs = []
    while True:
        try:
            msgs.append(alerter._queue.get_nowait())
        except queue.Empty:
            break
    return msgs


class TestTelegramNotifier(unittest.TestCase):
    def setUp(self):
        self.alerter = _make_alerter()

    def tearDown(self):
        self.alerter.stop()

    def test_notify_regime_gate_blocked(self):
        self.alerter.notify_regime_gate(
            underlying="BTC", dvol=42.0, iv_rank=58.0,
            blocked=True, reason="dvol=42<50",
        )
        msgs = _drain_queue(self.alerter)
        joined = "\n".join(msgs)
        self.assertIn("BLOCKED", joined)
        self.assertIn("BTC", joined)
        self.assertIn("dvol=42", joined)

    def test_notify_regime_gate_open(self):
        self.alerter.notify_regime_gate(
            underlying="ETH", dvol=55.0, iv_rank=45.0,
            blocked=False, reason="",
        )
        msgs = _drain_queue(self.alerter)
        joined = "\n".join(msgs)
        self.assertIn("OPEN", joined)
        self.assertIn("ETH", joined)

    def test_notify_expiry_auto_close(self):
        self.alerter.notify_expiry_auto_close(
            trade_id="t-123", underlying="BTC",
            strategy="short_strangle", expiry="2026-09-25",
        )
        msgs = _drain_queue(self.alerter)
        joined = "\n".join(msgs)
        self.assertIn("EXPIRY CLOSE", joined)
        self.assertIn("t-123", joined)
        self.assertIn("BTC", joined)
        self.assertIn("short_strangle", joined)
        self.assertIn("2026-09-25", joined)

    def test_notify_daily_summary_signs_pnl(self):
        # Realized loss: sign should be ''
        self.alerter.notify_daily_summary(
            cycle=100, n_trades=3, realized=-0.1234, unrealized=0.05,
        )
        msgs = _drain_queue(self.alerter)
        joined = "\n".join(msgs)
        self.assertIn("DAILY", joined)
        self.assertIn("realized=-0.1234", joined)
        self.assertIn("trades=3", joined)

    def test_disabled_alerter_drops_silently(self):
        """When env vars are missing, sends are no-ops."""
        alerter = TelegramAlerter(
            bot_token=None, chat_id=None, enabled=False,
        )
        # Should not raise, should not call _post_message (no thread started)
        alerter.notify_regime_gate("BTC", 50.0, 50.0, blocked=True)
        alerter.notify_expiry_auto_close("t", "BTC", "x", "2026-01-01")
        alerter.notify_daily_summary(0, 0, 0.0, 0.0)
        # _post_message is the real method, but no send ever went through the queue
        # so it should never have been called.
        self.assertFalse(alerter.enabled)

    def test_queue_full_drops_silently(self):
        """When queue is full, drops without raising."""
        # Pre-fill the queue to capacity
        for i in range(self.alerter._queue.maxsize):
            self.alerter._queue.put_nowait(f"spam-{i}")
        # Should not raise even though queue is full
        self.alerter.notify_regime_gate("BTC", 50.0, 50.0, blocked=True)
        self.alerter.notify_expiry_auto_close("t", "BTC", "x", "2026-01-01")
        self.alerter.notify_daily_summary(0, 0, 0.0, 0.0)


class TestTelegramNotifierConfig(unittest.TestCase):
    """Config-driven enablement (alerts.telegram.enabled flag)."""

    def test_alerts_yaml_auto_enables(self):
        """PaperRunner build path: if TELEGRAM_* env vars set, alerter is on."""
        import os
        old_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        old_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
        try:
            os.environ["TELEGRAM_BOT_TOKEN"] = "abc"
            os.environ["TELEGRAM_CHAT_ID"] = "999"
            a = TelegramAlerter()
            self.assertTrue(a.enabled)
            a.stop()
        finally:
            if old_token is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old_token
            if old_chat is not None:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat

    def test_no_env_vars_disabled(self):
        import os
        old_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        old_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
        try:
            a = TelegramAlerter()
            self.assertFalse(a.enabled)
        finally:
            if old_token is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old_token
            if old_chat is not None:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat


if __name__ == "__main__":
    unittest.main()
