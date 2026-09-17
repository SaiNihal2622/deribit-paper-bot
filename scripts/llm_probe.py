"""Manually probe every configured LLM provider. Used by ops + tests."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env explicitly (python-dotenv) so the probe works from any cwd.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(ROOT / ".env", override=False)
except Exception:  # noqa: BLE001
    pass

from crypto_options_bot.agent.llm import LLMClient  # noqa: E402


def main() -> int:
    client = LLMClient(settings_path=ROOT / "config" / "settings.yaml")
    print("Configured providers:")
    for st in client.provider_status():
        present = "yes" if st["key_present"] else "NO"
        print(f"  {st['name']:20}  model={st['model']:30}  key_env={st['api_key_env']:24}  key={present}")
    print()
    print("Probing each provider with a 1-token call...")
    ok = 0
    for st in client.provider_status():
        if not st["key_present"]:
            print(f"  skip   {st['name']:20}  (no key in env)")
            continue
        if st["dead_now"]:
            print(f"  skip   {st['name']:20}  (blacklisted)")
            continue
        try:
            r = client.messages(
                model=None,
                system="Reply with the single word OK.",
                messages=[{"role": "user", "content": "OK"}],
                max_tokens=4,
                provider=st["name"],
            )
            print(f"  OK     {st['name']:20}  text={r.text!r}  tokens={r.total_tokens}  provider={r.provider}")
            ok += 1
        except Exception as exc:
            msg = str(exc)[:140]
            print(f"  FAIL   {st['name']:20}  {msg}")
    print()
    print(f"Working: {ok}")
    return 0 if ok >= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
