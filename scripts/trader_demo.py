"""One-shot Trader LLM demo. Fires a real decision through the LLM
router using a representative Trader scenario."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # type: ignore
load_dotenv(ROOT / ".env", override=False)

from crypto_options_bot.agent.llm import LLMClient  # noqa: E402


SYSTEM = (
    "You are a careful crypto options trader for a Deribit paper-trading "
    "bot. Output STRICT JSON only, no prose, no markdown. Schema: "
    '{"action":"APPROVE|VETO|DOWNSIZE|HOLD","target_qty":<int 0-2>,'
    '"reason":"<one short sentence>"}.\n\n'
    "Hard rails: never widen RiskEngine caps, never change strategy "
    "eligibility, never place orders directly. VETO if the plan breaches "
    "the bot's hard rails. APPROVE only if risk and edge align."
)


SCENARIO = (
    "ETH 18SEP26 expiry. spot=4250.0 IV=0.65 IV rank=50 DVOL=51. "
    "Daily P&L=0.00 Open positions=4 of max 4 Preset=base. "
    "Strategy plan: short_strangle ETH 2 legs at delta=0.20 credit=0.1038. "
    "Should we APPROVE, VETO, DOWNSIZE, or HOLD this trade?"
)


def main() -> int:
    llm = LLMClient(settings_path=ROOT / "config" / "settings.yaml")
    print("=" * 60)
    print("  Trader agent — live LLM decision demo")
    print("=" * 60)
    print(f"Scenario: {SCENARIO}")
    print()
    resp = llm.messages(
        model=None,
        system=SYSTEM,
        messages=[{"role": "user", "content": SCENARIO}],
        max_tokens=200,
        temperature=0.2,
    )
    print(f"Provider: {resp.provider}")
    print(f"Model:    {resp.model}")
    print(f"Tokens:   in={resp.input_tokens} out={resp.output_tokens} total={resp.total_tokens}")
    print(f"Budget:   {json.dumps(llm.budget_snapshot(), default=str)}")
    print()
    print("Raw response:")
    print(f"  {resp.text}")
    print()

    # Try to parse the JSON
    try:
        parsed = json.loads(resp.text)
        print(f"Parsed JSON: {json.dumps(parsed, indent=2)}")
    except Exception as exc:
        print(f"(JSON parse failed: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
