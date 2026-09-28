"""test_two_switch_live_safety.py — verify the live-mode guard rejects without BOTH switches.

TradingXBot-style two-switch live-safety:
  1. DERIBIT_LIVE_CONFIRMED=YES  (explicit acknowledgment)
  2. LIVE_TRADING_ARMED_AT=<ISO8601 within last 24h>  (time-bounded arm)

Both required. The bot must refuse to start live trading if either
is missing, malformed, or stale.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

import pytest


def _run_cmd_live(env_overrides: dict[str, str | None]) -> subprocess.CompletedProcess:
    """Spawn `python -m crypto_options_bot live` with the given env overrides.

    Pass None to UNSET a variable from the base env. The subprocess
    captures stdout/stderr so we can assert the refusal reason.
    """
    base = os.environ.copy()
    # Required creds so we don't trigger that refusal too
    base["DERIBIT_CLIENT_ID"] = base.get("DERIBIT_CLIENT_ID", "test_id")
    base["DERIBIT_CLIENT_SECRET"] = base.get("DERIBIT_CLIENT_SECRET", "test_secret")
    for k, v in env_overrides.items():
        if v is None:
            base.pop(k, None)
        else:
            base[k] = v

    return subprocess.run(
        [sys.executable, "-m", "crypto_options_bot", "live"],
        capture_output=True,
        text=True,
        env=base,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        timeout=10,
    )


def test_refuses_without_switch1():
    """No DERIBIT_LIVE_CONFIRMED at all -> refused."""
    result = _run_cmd_live({
        "DERIBIT_LIVE_CONFIRMED": None,
        "LIVE_TRADING_ARMED_AT": datetime.now(timezone.utc).isoformat(),
    })
    assert result.returncode == 1
    assert "Switch 1" in result.stderr or "Switch 1" in result.stdout
    assert "DERIBIT_LIVE_CONFIRMED" in (result.stderr + result.stdout)


def test_refuses_without_switch2():
    """DERIBIT_LIVE_CONFIRMED=YES but no LIVE_TRADING_ARMED_AT -> refused."""
    result = _run_cmd_live({
        "DERIBIT_LIVE_CONFIRMED": "YES",
        "LIVE_TRADING_ARMED_AT": None,
    })
    assert result.returncode == 1
    assert "LIVE_TRADING_ARMED_AT" in (result.stderr + result.stdout)
    assert "Switch 2" in result.stderr or "Switch 2" in result.stdout


def test_refuses_with_stale_arm():
    """LIVE_TRADING_ARMED_AT 48h old -> refused."""
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    result = _run_cmd_live({
        "DERIBIT_LIVE_CONFIRMED": "YES",
        "LIVE_TRADING_ARMED_AT": stale,
    })
    assert result.returncode == 1
    assert "is" in (result.stderr + result.stdout) and "old" in (result.stderr + result.stdout)


def test_refuses_with_future_arm():
    """LIVE_TRADING_ARMED_AT 5 min in the future -> refused."""
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    result = _run_cmd_live({
        "DERIBIT_LIVE_CONFIRMED": "YES",
        "LIVE_TRADING_ARMED_AT": future,
    })
    assert result.returncode == 1
    assert "future" in (result.stderr + result.stdout)


def test_refuses_with_malformed_arm():
    """LIVE_TRADING_ARMED_AT not a valid ISO-8601 -> refused."""
    result = _run_cmd_live({
        "DERIBIT_LIVE_CONFIRMED": "YES",
        "LIVE_TRADING_ARMED_AT": "not-a-timestamp",
    })
    assert result.returncode == 1
    assert "not a valid ISO-8601" in (result.stderr + result.stdout)


def test_accepts_with_both_switches_fresh():
    """DERIBIT_LIVE_CONFIRMED=YES + LIVE_TRADING_ARMED_AT now -> should pass guard.

    We expect the guard to PASS (returncode != 1 from guard). The actual
    live mode will then try to connect to Deribit with our test creds and
    fail to authenticate, but that's a different error. We're only testing
    that the two-switch guard didn't block us.
    """
    fresh = datetime.now(timezone.utc).isoformat()
    result = _run_cmd_live({
        "DERIBIT_LIVE_CONFIRMED": "YES",
        "LIVE_TRADING_ARMED_AT": fresh,
    })
    # Guard passes if we DON'T see "Live mode refused"
    combined = result.stderr + result.stdout
    assert "Live mode refused" not in combined, (
        f"Guard should have passed with fresh arm. Got:\n{combined[:500]}"
    )


def test_arm_with_z_suffix_iso():
    """LIVE_TRADING_ARMED_AT=2026-09-29T01:00:00Z should be accepted.

    The 'Z' suffix is common from `date -u` and must be tolerated.
    """
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = _run_cmd_live({
        "DERIBIT_LIVE_CONFIRMED": "YES",
        "LIVE_TRADING_ARMED_AT": fresh,
    })
    combined = result.stderr + result.stdout
    assert "not a valid ISO-8601" not in combined


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
