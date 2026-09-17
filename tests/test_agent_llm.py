"""Tests for the agent layer."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from crypto_options_bot.agent.llm import LLMClient, LLMError, LLMResponse, LLMBudget


class FakeBudget(LLMBudget):
    def __init__(self) -> None:
        super().__init__(daily_token_limit=1_000_000, daily_call_limit=10_000)


def make_mock(text: str = "ok", charge: bool = False) -> LLMClient:
    """Mock LLMClient. If charge=True, charge budget on each call (mimics real path)."""
    def _mock(*_args: Any, **_kwargs: Any) -> LLMResponse:
        out = LLMResponse(text=text, model="mock/test", input_tokens=1, output_tokens=1)
        return out

    c = LLMClient(api_key="sk-test", base_url="http://mock.invalid", mock=_mock)
    if charge:
        # Wrap messages to also charge.
        original = c.messages
        def _and_charge(*a, **k):
            r = original(*a, **k)
            c.budget.charge(r.input_tokens + r.output_tokens)
            return r

        c.messages = _and_charge  # type: ignore[method-assign]
    return c


class TestLLMClient(unittest.TestCase):
    def test_mock_path_returns_text(self) -> None:
        c = make_mock("hello")
        r = c.messages(messages=[{"role": "user", "content": "ping"}])
        self.assertEqual(r.text, "hello")
        self.assertEqual(r.input_tokens, 1)

    def test_budget_charges_per_call(self) -> None:
        c = make_mock(charge=True)
        c.budget = FakeBudget()
        snap0 = c.budget_snapshot()
        c.messages(messages=[{"role": "user", "content": "a"}])
        c.messages(messages=[{"role": "user", "content": "b"}])
        snap1 = c.budget_snapshot()
        self.assertEqual(snap1["calls_used"] - snap0["calls_used"], 2)
        self.assertGreater(snap1["tokens_used"], snap0["tokens_used"])

    def test_missing_api_key_raises(self) -> None:
        c = LLMClient(api_key="", base_url="http://mock.invalid", mock=None)
        c.api_key = ""
        with self.assertRaises(LLMError):
            c.messages(messages=[{"role": "user", "content": "x"}])

    def test_strip_provider(self) -> None:
        self.assertEqual(LLMClient._strip_provider("minimax/MiniMax-M3"), "MiniMax-M3")
        self.assertEqual(LLMClient._strip_provider("MiniMax-M3"), "MiniMax-M3")

    def test_extract_text_anthropic_shape(self) -> None:
        data = {"content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]}
        self.assertEqual(LLMClient._extract_text(data), "hello world")

    def test_extract_tokens(self) -> None:
        data = {"usage": {"input_tokens": 12, "output_tokens": 34}}
        self.assertEqual(LLMClient._extract_tokens(data), (12, 34))

    def test_budget_rollover(self) -> None:
        b = LLMBudget(daily_token_limit=10, daily_call_limit=2)
        b.tokens_used = 9
        b.calls_used = 2
        b._day_started_at = 1  # epoch-1, definitely > 24h old
        # exceeded() triggers rollover then evaluates 0 >= 10 = False
        self.assertFalse(b.exceeded())
        snap = b.snapshot()
        self.assertEqual(snap["tokens_used"], 0)
        self.assertEqual(snap["calls_used"], 0)


if __name__ == "__main__":
    unittest.main()
