"""Tests for the Deribit WS keepalive (heartbeat) loop.

Background: Deribit's WSS closes idle connections after ~2 minutes. The bot
fires reconnects every 2 minutes as a side-effect, which spams the bot log
with "Connection to remote host was lost" warnings. We now have a
separate keepalive thread that sends `public/test_request` every 60s.

These tests cover the keepalive thread's lifecycle + heartbeat behaviour
without needing a real network. They use ``max_iterations`` on the loop
so an infinite-loop method stays bounded under test.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.data.deribit_ws import DeribitWebSocketFeed


class TestKeepaliveThreadLifecycle:
    def test_start_spawns_keepalive_thread(self):
        """The feed must expose a keepalive thread slot and loop method."""
        feed = DeribitWebSocketFeed(env="testnet")
        assert hasattr(feed, "_keepalive_thread")
        assert hasattr(feed, "_keepalive_loop")

    def test_ping_interval_default(self):
        """Default ping interval should be 60s — well under Deribit's 2-min idle."""
        feed = DeribitWebSocketFeed(env="testnet")
        assert feed._ping_interval_sec == 60.0

    def test_stop_joins_keepalive_thread(self):
        """stop() must join the keepalive thread (with timeout) to avoid orphans."""
        feed = DeribitWebSocketFeed(env="testnet")
        fake = MagicMock(spec=threading.Thread)
        feed._keepalive_thread = fake
        with patch.object(feed, "_close_ws"), \
             patch.object(threading.Thread, "join", lambda self, timeout=0: None), \
             patch.object(feed, "_thread", None):
            feed.stop()
        fake.join.assert_called_with(timeout=2)


class TestKeepaliveHeartbeatSend:
    def test_sends_test_request_when_connected(self):
        """When connected, keepalive must call ws.send() with public/test_request."""
        feed = DeribitWebSocketFeed(env="testnet")
        feed._connected = True
        mock_ws = MagicMock()
        feed._ws = mock_ws
        feed._running = True
        with patch("crypto_options_bot.data.deribit_ws.time.sleep"):
            feed._keepalive_loop(max_iterations=1)
        assert mock_ws.send.called, "ws.send was not called from keepalive"
        # The message must include the heartbeat method
        args, _ = mock_ws.send.call_args
        assert "public/test_request" in args[0]

    def test_does_not_send_when_disconnected(self):
        """When disconnected, keepalive must not call ws.send()."""
        feed = DeribitWebSocketFeed(env="testnet")
        feed._connected = False
        mock_ws = MagicMock()
        feed._ws = mock_ws
        feed._running = True
        with patch("crypto_options_bot.data.deribit_ws.time.sleep"):
            feed._keepalive_loop(max_iterations=2)
        assert not mock_ws.send.called, "ws.send called while disconnected"

    def test_does_not_raise_when_send_fails(self):
        """If ws.send raises, keepalive must swallow and keep looping."""
        feed = DeribitWebSocketFeed(env="testnet")
        feed._connected = True
        mock_ws = MagicMock()
        mock_ws.send.side_effect = OSError("broken pipe")
        feed._ws = mock_ws
        feed._running = True
        with patch("crypto_options_bot.data.deribit_ws.time.sleep"):
            # Three iterations: ensure we get past the first failure
            feed._keepalive_loop(max_iterations=3)
        # send() was attempted at least three times
        assert mock_ws.send.call_count >= 3


class TestReconnectPerformance:
    def test_reconnects_not_spammed_when_keepalive_present(self):
        """Sanity: 60s ping < Deribit's ~2-min idle, and loop method exists."""
        feed = DeribitWebSocketFeed(env="testnet")
        assert feed._ping_interval_sec < 120, (
            "ping_interval must be < Deribit's ~2-min idle timeout"
        )
        assert hasattr(feed, "_keepalive_loop"), (
            "keepalive loop method missing — bot will spam reconnects"
        )
