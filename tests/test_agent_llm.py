"""Tests for the multi-provider LLM client."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

from crypto_options_bot.agent.llm import (
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMResponse,
    LLMBudget,
    Provider,
    DEFAULT_PROVIDERS,
)


class FakeBudget(LLMBudget):
    def __init__(self) -> None:
        super().__init__(daily_token_limit=1_000_000, daily_call_limit=10_000)


def make_mock(text: str = "ok") -> LLMClient:
    def _mock(*_args: Any, **_kwargs: Any) -> LLMResponse:
        return LLMResponse(text=text, model="mock/test", input_tokens=1, output_tokens=1)

    return LLMClient(api_key="sk-test", base_url="http://mock.invalid", mock=_mock)


class TestLLMBudget(unittest.TestCase):
    def test_spend_and_rollover(self) -> None:
        b = LLMBudget(daily_token_limit=100, daily_call_limit=5)
        b.charge(20)
        b.charge(30)
        self.assertEqual(b.snapshot()["spent_tokens_today"], 50)
        self.assertEqual(b.snapshot()["calls_today"], 2)
        self.assertFalse(b.exceeded())

        b._date = "1999-01-01"  # force rollover
        self.assertFalse(b.exceeded())
        self.assertEqual(b.snapshot()["spent_tokens_today"], 0)

    def test_exceeded(self) -> None:
        b = LLMBudget(daily_token_limit=10, daily_call_limit=2)
        b.charge(10)
        b.charge(1)
        self.assertTrue(b.exceeded())


class TestLLMClient(unittest.TestCase):
    def test_mock_path_returns_text(self) -> None:
        c = make_mock("hello")
        r = c.messages(messages=[{"role": "user", "content": "ping"}])
        self.assertEqual(r.text, "hello")
        self.assertEqual(r.input_tokens, 1)

    def test_budget_charges_via_real_call(self) -> None:
        # We need a real call (not mock) to exercise budget charging path.
        c = LLMClient(api_key="sk-test", base_url="http://mock.invalid")
        captured: dict[str, Any] = {}

        def _fake_post(self: Any, url: str, headers: Any = None, json: Any = None, **kw: Any):  # type: ignore[no-untyped-def]
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json

            class R:
                status_code = 200

                def json(self_inner) -> dict[str, Any]:
                    return {
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 3, "output_tokens": 7},
                    }

            return R()

        with mock.patch.object(httpx.Client, "post", _fake_post):  # type: ignore[name-defined]
            r = c.messages(messages=[{"role": "user", "content": "ping"}])
        self.assertEqual(r.text, "ok")
        self.assertEqual(r.input_tokens, 3)
        self.assertEqual(r.output_tokens, 7)
        self.assertEqual(r.total_tokens, 10)
        self.assertEqual(c.budget.snapshot()["spent_tokens_today"], 10)
        self.assertEqual(c.budget.snapshot()["calls_today"], 1)
        self.assertIn("/v1/messages", captured["url"])

    def test_missing_api_key_raises(self) -> None:
        # With no providers configured and no env var set, messages() raises
        # because there's no provider with a key.
        os.environ.pop("MINIMAX_API_KEY", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("MISTRAL_API_KEY", None)
        with tempfile.TemporaryDirectory() as d:
            c = LLMClient(settings_path=Path(d) / "no-such.yaml")
            with self.assertRaises(LLMError):
                c.messages(messages=[{"role": "user", "content": "x"}])

    def test_strip_provider(self) -> None:
        self.assertEqual(LLMClient._strip_provider("minimax/MiniMax-M3"), "MiniMax-M3")
        self.assertEqual(LLMClient._strip_provider("MiniMax-M3"), "MiniMax-M3")

    def test_default_providers_include_minimax_and_known_alternatives(self) -> None:
        names = [d["name"] for d in DEFAULT_PROVIDERS]
        for required in ("minimax-proxy", "anthropic", "openrouter", "groq", "mistral"):
            self.assertIn(required, names)

    def test_provider_status(self) -> None:
        c = LLMClient(api_key="sk-test", base_url="http://mock.invalid")
        # Legacy mode → only the inline provider.
        statuses = c.provider_status()
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["name"], "legacy-inline")
        self.assertTrue(statuses[0]["key_present"])


class TestMultiProviderRouter(unittest.TestCase):
    """Verify the router tries providers in order, blacklists auth failures,
    caches last-known-good."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / "settings.yaml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_settings(self, providers_cfg: list[dict[str, Any]]) -> None:
        self._path.write_text(
            "agent:\n  providers:\n"
            + "\n".join(f"    - {json.dumps(p)}" for p in providers_cfg)
            + "\n",
            encoding="utf-8",
        )

    def test_falls_back_to_second_provider_on_auth_error(self) -> None:
        self._write_settings([
            {
                "name": "bad",
                "base_url": "http://bad.invalid",
                "model": "bad-model",
                "protocol": "messages",
                "auth": "bearer",
                "api_key_env": "BAD_KEY",
            },
            {
                "name": "good",
                "base_url": "http://good.invalid",
                "model": "good-model",
                "protocol": "messages",
                "auth": "bearer",
                "api_key_env": "GOOD_KEY",
            },
        ])
        os.environ["BAD_KEY"] = "sk-bad"
        os.environ["GOOD_KEY"] = "sk-good"

        c = LLMClient(settings_path=self._path)

        call_log: list[str] = []

        def _fake_post(self: Any, url: str, headers: Any = None, json: Any = None, **kw: Any):  # type: ignore[no-untyped-def]
            call_log.append(url)

            class R:
                status_code = 200 if "good.invalid" in url else 401
                text = ""

                def json(self_inner) -> dict[str, Any]:
                    if "good.invalid" in url:
                        return {
                            "content": [{"type": "text", "text": "yes"}],
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        }
                    return {"error": "auth"}

            return R()

        with mock.patch.object(httpx.Client, "post", _fake_post):  # type: ignore[name-defined]
            r = c.messages(messages=[{"role": "user", "content": "x"}])

        self.assertEqual(r.text, "yes")
        self.assertEqual(r.provider, "good")
        self.assertTrue(any("bad.invalid" in u for u in call_log))
        self.assertTrue(any("good.invalid" in u for u in call_log))
        self.assertGreater(c._dead_providers.get("bad", 0), 0)

    def test_routes_to_provider_by_name(self) -> None:
        self._write_settings([
            {
                "name": "first",
                "base_url": "http://first.invalid",
                "model": "first-model",
                "protocol": "messages",
                "auth": "bearer",
                "api_key_env": "FIRST_KEY",
            },
            {
                "name": "second",
                "base_url": "http://second.invalid",
                "model": "second-model",
                "protocol": "messages",
                "auth": "bearer",
                "api_key_env": "SECOND_KEY",
            },
        ])
        os.environ["FIRST_KEY"] = "sk-first"
        os.environ["SECOND_KEY"] = "sk-second"
        c = LLMClient(settings_path=self._path)

        hit: list[str] = []

        def _fake_post(self: Any, url: str, headers: Any = None, json: Any = None, **kw: Any):  # type: ignore[no-untyped-def]
            hit.append(url)

            class R:
                status_code = 200

                def json(self_inner) -> dict[str, Any]:
                    return {
                        "content": [{"type": "text", "text": "forced"}],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }

            return R()

        with mock.patch.object(httpx.Client, "post", _fake_post):  # type: ignore[name-defined]
            r = c.messages(
                provider="second",
                messages=[{"role": "user", "content": "x"}],
            )
        self.assertEqual(r.provider, "second")
        self.assertEqual(len(hit), 1)
        self.assertIn("second.invalid", hit[0])

    def test_chat_completions_protocol(self) -> None:
        self._write_settings([
            {
                "name": "openai",
                "base_url": "https://api.example.com/v1",
                "model": "gpt-4o-mini",
                "protocol": "chat-completions",
                "auth": "bearer",
                "api_key_env": "OPENAI_TEST",
            },
        ])
        os.environ["OPENAI_TEST"] = "sk-test"
        c = LLMClient(settings_path=self._path)

        captured: dict[str, Any] = {}

        def _fake_post(self: Any, url: str, headers: Any = None, json: Any = None, **kw: Any):  # type: ignore[no-untyped-def]
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json

            class R:
                status_code = 200

                def json(self_inner) -> dict[str, Any]:
                    return {
                        "choices": [{"message": {"content": "hi"}}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 9},
                    }

            return R()

        with mock.patch.object(httpx.Client, "post", _fake_post):  # type: ignore[name-defined]
            r = c.messages(
                messages=[{"role": "user", "content": "yo"}],
                system="be brief",
            )
        self.assertEqual(r.text, "hi")
        self.assertEqual(r.input_tokens, 5)
        self.assertEqual(r.output_tokens, 9)
        self.assertEqual(r.provider, "openai")
        self.assertIn("/chat/completions", captured["url"])
        self.assertEqual(captured["body"]["model"], "gpt-4o-mini")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-test")
        roles = [m["role"] for m in captured["body"]["messages"]]
        self.assertIn("system", roles)


class TestProviderAuthSchemes(unittest.TestCase):
    def test_bearer_header(self) -> None:
        c = LLMClient(api_key="sk-x", base_url="http://x.invalid")
        p = Provider(
            name="t", base_url="http://x.invalid", model="m",
            auth="bearer", api_key_env="X"
        )
        p.api_key = "sk-xyz"
        hdrs = c._build_headers(p, None)
        self.assertEqual(hdrs["Authorization"], "Bearer sk-xyz")

    def test_x_api_key_header(self) -> None:
        c = LLMClient(api_key="sk-x", base_url="http://x.invalid")
        p = Provider(
            name="t", base_url="http://x.invalid", model="m",
            auth="x-api-key", api_key_env="X",
            headers={"anthropic-version": "2023-06-01"},
        )
        p.api_key = "sk-xyz"
        hdrs = c._build_headers(p, None)
        self.assertEqual(hdrs["x-api-key"], "sk-xyz")
        self.assertEqual(hdrs["anthropic-version"], "2023-06-01")


if __name__ == "__main__":
    unittest.main()
