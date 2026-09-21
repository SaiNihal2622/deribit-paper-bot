"""One-shot validation: force-fire Evolver + Reflector once and print what they produced.

Use this to confirm the agent layer is observably alive even when the
scheduled ticks haven't reached their next fire time. Idempotent: safe to
re-run. Does NOT auto-deploy any proposal — instead, reports each
proposal's risk class so a human can decide.

Usage:
    python -m scripts.run_agents_once
    python -m scripts.run_agents_once --dry-run   # do not deploy even if autodeploy=True
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.agent.evolver import Evolver  # noqa: E402
from crypto_options_bot.agent.llm import LLMClient    # noqa: E402
from crypto_options_bot.agent.memory import Memory    # noqa: E402
from crypto_options_bot.agent.reflector import Reflector  # noqa: E402


def _build_components():
    settings = ROOT / "config" / "settings.yaml"
    mem = Memory(root=ROOT / "memory")
    llm = LLMClient(settings_path=settings)
    return mem, llm


def _print_section(name: str, payload: dict) -> None:
    print(f"\n=== {name} ===")
    print(json.dumps(payload, default=str, indent=2))


def run_evolver(dry_run: bool) -> dict:
    mem, llm = _build_components()
    evo = Evolver(project_root=ROOT, memory=mem, llm=llm)
    proposals = evo.run_once()
    out = {
        "produced": len(proposals),
        "proposals": [
            {
                "id": p.proposal_id,
                "summary": p.summary,
                "risk": p.risk,
                "autodeploy": p.autodeploy,
                "diff": p.diff,
            }
            for p in proposals
        ],
    }
    if dry_run:
        out["note"] = "dry-run: autodeploy disabled; review proposals manually"
    return out


def run_reflector(dry_run: bool) -> dict:
    mem, llm = _build_components()
    ref = Reflector(project_root=ROOT, memory=mem, llm=llm)
    out_path = ref.run_once()
    return {
        "summary_path": str(out_path) if out_path else None,
        "existed": bool(out_path and out_path.exists()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip any settings.yaml mutations (Evolver autodeploy disabled)")
    ap.add_argument("--skip-evolver", action="store_true")
    ap.add_argument("--skip-reflector", action="store_true")
    args = ap.parse_args()

    rc = 0
    try:
        if not args.skip_evolver:
            r = run_evolver(dry_run=args.dry_run)
            _print_section("EVOLVER", r)
        if not args.skip_reflector:
            r = run_reflector(dry_run=args.dry_run)
            _print_section("REFLECTOR", r)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
