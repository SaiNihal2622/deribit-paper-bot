"""Tests for the Reflector agent."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crypto_options_bot.agent.llm import LLMClient, LLMResponse
from crypto_options_bot.agent.memory import Memory
from crypto_options_bot.agent.reflector import Reflector


def _llm(reply: str) -> LLMClient:
    def _mock(*_args, **_kwargs):
        return LLMResponse(text=reply, model="mock", input_tokens=1, output_tokens=1)

    return LLMClient(api_key="sk-test", mock=_mock)


def _write_prompt(project_root: Path) -> None:
    pd = project_root / "crypto_options_bot" / "agent" / "prompts"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "reflector_daily.md").write_text("CTX={{CONTEXT}}", encoding="utf-8")


class TestReflector(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mem = Memory(root=self.root / "memory")
        _write_prompt(self.root)
        self.ref = Reflector(project_root=self.root, memory=self.mem, llm=_llm("# Today\n\n## What went well\nNone."))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_run_once_writes_lesson(self) -> None:
        # Seed some history so the reflector has something to reflect on.
        self.mem.append_history("trader_decisions", {"ts": 1, "action": "approve"})
        self.mem.write_health({"bot_alive": True})

        path = self.ref.run_once()
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("## What went well", text)

    def test_skips_when_no_activity(self) -> None:
        # No history written; nothing to reflect on.
        path = self.ref.run_once()
        self.assertIsNone(path)

    def test_extract_title(self) -> None:
        self.assertEqual(Reflector._extract_title("# My Title\nbody"), "My Title")
        self.assertEqual(Reflector._extract_title("plain text"), None)

    def test_missing_prompt_returns_none(self) -> None:
        (self.root / "crypto_options_bot" / "agent" / "prompts" / "reflector_daily.md").unlink()
        self.mem.append_history("trader_decisions", {"action": "veto"})
        self.assertIsNone(self.ref.run_once())

    def test_llm_error_returns_none(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("LLM offline")

        self.ref.llm = LLMClient(api_key="sk-test", mock=boom)
        self.mem.append_history("trader_decisions", {"action": "veto"})
        self.assertIsNone(self.ref.run_once())


if __name__ == "__main__":
    unittest.main()
