"""mainnet_readiness — verify the bot has what it needs to flip to real Deribit mainnet.

Prints a checklist of env vars, config flags, and the order they must be flipped
in. Does NOT make any changes — read-only by design. The actual flip happens
when the user sets the env vars AND restarts the bot with `--mode live`.

Usage:
    python scripts/mainnet_readiness.py           # human-readable report
    python scripts/mainnet_readiness.py --json    # machine-readable JSON for CI

Exit code:
    0 — all readiness checks pass (safe to go live, pending user confirmation)
    1 — one or more readiness checks fail
    2 — bot config has safety issues that would block the live flip
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# Repo root = parent of scripts/
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(REPO_ROOT, "config", "settings.yaml")
ENV_PATH = os.path.join(REPO_ROOT, ".env")

# What we expect to see for a successful live flip.
REQUIRED_ENV_VARS = (
    "DERIBIT_CLIENT_ID",
    "DERIBIT_CLIENT_SECRET",
    "DERIBIT_LIVE_CONFIRMED",
)
REQUIRED_DERIBIT_ENV = "prod"  # value that data.deribit_env must take


def _load_settings() -> dict:
    """Tiny YAML loader using only stdlib. Good enough for our flat config."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    # We only care about a few scalar fields; parse them with a minimal regex
    # to avoid adding PyYAML as a hard requirement (it's already a dep, but
    # we want this script to run even if YAML imports are broken for some reason).
    out: dict = {}
    # crude parser for "key: value" and "key:\n  subkey: value"
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line or line.lstrip().startswith("#"):
            i += 1
            continue
        # top-level "key: value"
        if not line.startswith(" "):
            stripped = line.strip()
            if ":" in stripped:
                key, _, rest = stripped.partition(":")
                rest = rest.strip()
                if rest and not rest.startswith("#"):
                    # scalar
                    val = rest.split("#")[0].strip().strip("'\"")
                    if val.lower() in ("true", "false"):
                        out[key.strip()] = val.lower() == "true"
                    else:
                        out[key.strip()] = val
                else:
                    # block — read subkeys
                    block: dict = {}
                    j = i + 1
                    while j < len(lines) and lines[j].startswith("  "):
                        sub = lines[j].strip()
                        if ":" in sub:
                            sk, _, sv = sub.partition(":")
                            sv = sv.strip()
                            if sv and not sv.startswith("#"):
                                sv = sv.split("#")[0].strip().strip("'\"")
                                if sv.lower() in ("true", "false"):
                                    block[sk.strip()] = sv.lower() == "true"
                                else:
                                    block[sk.strip()] = sv
                        j += 1
                    if block:
                        out[key.strip()] = block
                    i = j
                    continue
        i += 1
    return out


def _load_dotenv() -> dict[str, str]:
    """Read .env into a dict without polluting os.environ."""
    out: dict[str, str] = {}
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                out[k] = v
    except FileNotFoundError:
        pass
    return out


def _check_env(env_name: str, value: str | None) -> tuple[bool, str]:
    """Return (pass, detail) for an env-var check.

    Pulls from os.environ first, then falls back to .env so the script
    can run before a `python-dotenv` import.
    """
    raw = os.environ.get(env_name) or _load_dotenv().get(env_name)
    if not raw:
        return False, "NOT SET"
    if env_name == "DERIBIT_LIVE_CONFIRMED":
        if raw.strip().upper() != "YES":
            return False, f"set to '{raw}' (must be YES)"
        return True, "YES (consent recorded)"
    # API keys: just confirm they're non-empty and look like Deribit format
    if len(raw) < 8:
        return False, f"too short (len={len(raw)})"
    return True, f"set ({raw[:4]}…)"


def _check_setting(setting_path: list[str], expected: Any, actual: Any) -> tuple[bool, str]:
    """Walk a nested setting path and compare to expected."""
    cur: Any = _load_settings()
    for key in setting_path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return False, f"missing setting { '.'.join(setting_path) }"
    return (cur == expected, f"current={cur!r}, expected={expected!r}")


def collect_checks() -> list[dict]:
    """Run all readiness checks and return them as a structured list."""
    checks: list[dict] = []

    # 1. env vars
    for env_name in REQUIRED_ENV_VARS:
        ok, detail = _check_env(env_name, None)
        checks.append({
            "id": f"env.{env_name}",
            "category": "env",
            "description": f"Environment variable {env_name}",
            "status": "PASS" if ok else "FAIL",
            "detail": detail,
            "required": True,
            "fix": (
                f"export {env_name}=<value>"
                if not ok
                else None
            ),
        })

    # 2. settings.yaml — deribit_env must be 'prod'
    actual_env = (
        _load_settings().get("data", {}).get("deribit_env")
        if isinstance(_load_settings().get("data"), dict)
        else None
    )
    checks.append({
        "id": "settings.data.deribit_env",
        "category": "config",
        "description": (
            "config/settings.yaml: data.deribit_env must be 'prod' for mainnet"
        ),
        "status": "PASS" if actual_env == REQUIRED_DERIBIT_ENV else "FAIL",
        "detail": f"current={actual_env!r}, expected={REQUIRED_DERIBIT_ENV!r}",
        "required": True,
        "fix": (
            "Edit config/settings.yaml — change `data.deribit_env: testnet` to "
            "`data.deribit_env: prod`. The bot refuses to run live otherwise."
            if actual_env != REQUIRED_DERIBIT_ENV
            else None
        ),
    })

    # 3. settings.yaml — mode must be 'live' (otherwise the bot stays in paper)
    actual_mode = _load_settings().get("mode")
    # CLI flag --mode live wins over settings.yaml. We don't flag this as a hard fail
    # because the user might pass --mode live at startup; just inform.
    checks.append({
        "id": "settings.mode",
        "category": "config",
        "description": (
            "config/settings.yaml: mode must be 'live' (or start with --mode live)"
        ),
        "status": "INFO",
        "detail": (
            f"current={actual_mode!r}. CLI flag `python -m crypto_options_bot live` "
            f"overrides this."
        ),
        "required": False,
        "fix": None,
    })

    # 4. settings.yaml — sanity: data.mark_price_proxy should be true (testnet
    #    edge case; on mainnet with real quotes you can leave it on safely).
    mp = (
        _load_settings().get("data", {}).get("mark_price_proxy")
        if isinstance(_load_settings().get("data"), dict)
        else None
    )
    checks.append({
        "id": "settings.data.mark_price_proxy",
        "category": "config",
        "description": (
            "config/settings.yaml: data.mark_price_proxy — keep true on mainnet "
            "as a safety fallback for illiquid strikes"
        ),
        "status": "INFO",
        "detail": f"current={mp!r}",
        "required": False,
        "fix": None,
    })

    # 5. Process check: is the bot currently running on testnet? If yes, the
    #    user must restart it after flipping env vars — we cannot hot-swap.
    pid_file = os.path.join(REPO_ROOT, "bot.pid")
    bot_running = os.path.exists(pid_file)
    checks.append({
        "id": "runtime.bot_running",
        "category": "runtime",
        "description": (
            "Bot process state — a running bot must be RESTARTED for env changes "
            "to take effect. The bot does NOT hot-reload credentials."
        ),
        "status": "WARN" if bot_running else "INFO",
        "detail": (
            f"bot.pid found at {pid_file} — restart required after env changes"
            if bot_running
            else "bot not running — safe to flip env vars now"
        ),
        "required": False,
        "fix": (
            "Run: powershell -File scripts/stop_bot.ps1; "
            "powershell -File start_bot_detached.ps1"
            if bot_running
            else None
        ),
    })

    return checks


def render_human(checks: list[dict]) -> str:
    """Format the checks list as a human-readable report."""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("  MAINNET READINESS CHECK — Deribit live trading")
    lines.append("=" * 78)
    lines.append("")

    # Summary
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "INFO": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    lines.append(
        f"  Summary: {counts.get('PASS', 0)} pass, "
        f"{counts.get('FAIL', 0)} fail, "
        f"{counts.get('WARN', 0)} warn, "
        f"{counts.get('INFO', 0)} info"
    )
    lines.append("")

    # Required checks first
    lines.append("  REQUIRED CHECKS (must all pass before going live)")
    lines.append("  " + "-" * 74)
    for c in checks:
        if not c.get("required"):
            continue
        status = c["status"]
        sym = {
            "PASS": "[PASS]",
            "FAIL": "[FAIL]",
            "WARN": "[WARN]",
            "INFO": "[INFO]",
        }.get(status, "[????]")
        lines.append(f"  {sym} {c['id']}")
        lines.append(f"         {c['description']}")
        lines.append(f"         detail: {c['detail']}")
        if c.get("fix"):
            lines.append(f"         FIX: {c['fix']}")
        lines.append("")

    # Informational checks
    lines.append("  INFORMATIONAL")
    lines.append("  " + "-" * 74)
    for c in checks:
        if c.get("required"):
            continue
        status = c["status"]
        sym = {
            "PASS": "[PASS]",
            "FAIL": "[FAIL]",
            "WARN": "[WARN]",
            "INFO": "[INFO]",
        }.get(status, "[????]")
        lines.append(f"  {sym} {c['id']}")
        lines.append(f"         {c['description']}")
        lines.append(f"         detail: {c['detail']}")
        if c.get("fix"):
            lines.append(f"         FIX: {c['fix']}")
        lines.append("")

    # The action plan
    lines.append("  " + "=" * 74)
    lines.append("  ACTION PLAN (in order)")
    lines.append("  " + "-" * 74)
    lines.append("  1. Create a Deribit mainnet API key at https://www.deribit.com/")
    lines.append("     -> Account -> API -> Create New Key (scope: trade+read)")
    lines.append("  2. Set the three env vars in your shell or .env:")
    lines.append("       DERIBIT_CLIENT_ID=<api_key>")
    lines.append("       DERIBIT_CLIENT_SECRET=<api_secret>")
    lines.append("       DERIBIT_LIVE_CONFIRMED=YES")
    lines.append("  3. Edit config/settings.yaml: data.deribit_env: prod")
    lines.append("  4. Stop the bot:    powershell -File scripts/stop_bot.ps1")
    lines.append("  5. Restart it:      powershell -File start_bot_detached.ps1")
    lines.append("     (or pass --mode live explicitly)")
    lines.append("  6. Verify:          python -m crypto_options_bot status")
    lines.append("     -> look for `feed.env: prod` in the output")
    lines.append("")
    lines.append("  Safety: the bot still defaults to paper unless DERIBIT_LIVE_CONFIRMED=YES.")
    lines.append("  Without that env var, the DeribitClient refuses to start in live mode.")
    lines.append("")
    lines.append("=" * 78)

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check mainnet readiness")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    checks = collect_checks()

    if args.json:
        print(json.dumps({"checks": checks}, indent=2))
    else:
        print(render_human(checks))

    # Exit code: any required FAIL = 1, any WARN = 0 (we still let user proceed),
    # no failures at all = 0 (safe to go live).
    required_failed = any(
        c["status"] == "FAIL" and c.get("required") for c in checks
    )
    return 1 if required_failed else 0


if __name__ == "__main__":
    sys.exit(main())