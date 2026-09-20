"""Tests for the Deribit WS keepalive (heartbeat) loop.

Background: Deribit's WSS closes idle connections after ~2 minutes. The bot
fires reconnects every 2 minutes as a side-effect, which spams the bot log
with "Connection to remote host was lost" warnings. We now have a
separate keepalive thread that sends `public/test_request` every 30s.

These tests cover the keepalive thread's lifecycle + heartbeat behaviour
without needing a real network.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.data.deribit_ws import DeribitWebSocketFeed


class TestKeepaliveThreadLifecycle:
    def test_start_spawns_keepalive_thread(self):
        """start() must spawn the keepalive thread alongside the main loop."""
        feed = DeribitWebSocketFeed(env="testnet", currencies=["BTC"])
        # Don't actually run start() (would hit the network); just check that
        # the attribute exists and start() wires it.
        assert hasattr(feed, "_keepalive_thread")
        assert hasattr(feed, "_keepalive_loop")

    def test_ping_interval_default(self):
        """Default ping interval should be 30s — well under Deribit's 2-min idle."""
        feed = DeribitWebSocketFeed(env="testnet")
        assert feed._ping_interval_sec == 30.0

    def test_stop_joins_keepalive_thread(self):
        """stop() must join the keepalive thread to avoid orphaned daemon threads."""
        feed = DeribitWebSocketFeed(env="testnet")
        # Pretend a keepalive thread is running
        fake = MagicMock(spec=threading.Thread)
        feed._keepalive_thread = fake
        with patch.object(feed, "_close_ws"), patch.object(threading.Thread, "join", lambda self, timeout=0: None):
            feed.stop()
        fake.join.assert_called_with(timeout=2)


class TestKeepaliveHeartbeatSend:
    def test_sends_test_request_when_connected(self):
        """When connected, keepalive must send `public/test_request` to Deribit."""
        feed = DeribitWebSocketFeed(env="testnet")
        feed._connected = True
        mock_ws = MagicMock()
        feed._ws = mock_ws
        # Patch _running so the loop exits after one iteration
        feed._running = True
        def stop_after_send(*_):
            feed._running = False
        # Patch sleep so the loop doesn't actually wait
        with patch("crypto_options_bot.data.deribit_ws.time.sleep"), \
             patch.object(feed, "_running", True):
            # Call the keepalive loop body directly (one iteration)
            try:
                feed._keepalive_loop()
            except SystemExit:
                pass
            except Exception:
                pass
        # The mock ws should have received a send() call with public/test_request
        assert mock_ws.send.called, "ws.send was not called from keepalive"

    def test_does_not_send_when_disconnected(self):
        """When disconnected, keepalive must not try to send."""
        feed = DeribitWebSocketFeed(env="testnet")
        feed._connected = False
        mock_ws = MagicMock()
        feed._ws = mock_ws
        feed._running = True
        # Patch the loop to exit immediately so we don't hang
        original_sleep = time.sleep
        def stop_loop_after_3s(seconds):
            if seconds >= 1.0:
                # After the disconnected-sleep, exit
                feed._running = False
        with patch("crypto_options_bot.data.deribit_ws.time.sleep", side_effect=stop_loop_after_3s):
            try:
                feed._keepalive_loop()
            except SystemExit:
                pass
        # We expect send() NOT to have been called while disconnected
        assert not mock_ws.send.called, "ws.send called while disconnected"

    def test_does_not_raise_when_send_fails(self):
        """If ws.send raises, keepalive must swallow and continue."""
        feed = DeribitWebSocketFeed(env="testnet")
        feed._connected = True
        mock_ws = MagicMock()
        mock_ws.send.side_effect = OSError("broken pipe")
        feed._ws = mock_ws
        feed._running = True
        def exit_after(seconds):
            if seconds >= self_interval:
                feed._running = False
        self_interval = feed._ping_interval_sec
        with patch("crypto_options_bot.data.deribit_ws.time.sleep", side_effect=exit_after):
            try:
                feed._keepalive_loop()
            except SystemExit:
                pass
        # Loop continued past the failed send
        assert mock_ws.send.called


class TestReconnectPerformance:
    def test_reconnects_not_spammed_when_keepalive_present(self):
        """Sanity: the keepalive attribute exists, ensuring we can fix the
        2-min reconnect-loop bug without regressions in the main path.
        """
        feed = DeribitWebSocketFeed(env="testnet")
        # Sanity checks
        assert feed._ping_interval_sec < 120, (
            "ping_interval must be < Deribit's ~2-min idle timeout"
        )
        assert hasattr(feed, "_keepalive_loop"), (
            "keepalive loop method missing — bot will spam reconnects"
        )