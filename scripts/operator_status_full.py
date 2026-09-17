"""One-shot: build an Operator in-process, run the LLM probe tick,
print provider status + the full status dict. Useful for diagnostics
when the operator is up but you want to see the LLM state immediately."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # type: ignore
load_dotenv(ROOT / ".env", override=False)

from crypto_options_bot.agent.operator import Operator, OperatorConfig  # noqa: E402


def main() -> int:
    cfg = OperatorConfig(
        project_root=ROOT,
        memory_dir=ROOT / "memory",
    )
    op = Operator(config=cfg)
    op._tick_llm_probe()

    print("=" * 60)
    print("  Operator LLM provider status")
    print("=" * 60)
    for s in op.llm.provider_status():
        present = "yes" if s["key_present"] else "NO"
        dead = "DEAD" if s["dead_now"] else "live"
        print(f"  {s['name']:35}  key={present:3}  {dead:5}  model={s['model']}")

    print()
    print("=" * 60)
    print("  Full operator status (excerpt)")
    print("=" * 60)
    s = op.status()
    print(f"  LLM providers: {len(s.get('llm_providers', []))}")
    print(f"  LLM budget:    {json.dumps(s.get('llm_budget', {}))}")
    print(f"  LLM last probe: {s.get('llm_last_probe_at', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
