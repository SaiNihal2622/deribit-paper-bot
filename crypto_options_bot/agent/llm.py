"""LLM client wrapper for MiniMax's Anthropic-compatible API.

Reuses the local ``.minimax/.builtin-skills/llm-call/scripts/llm_call.py``
endpoint and protocol mapping so we stay consistent with the host
agent. The wrapper:

    * Reads ``C:/Users/saini/.minimax/config.yaml`` automatically.
    * Honours ``MINIMAX_API_KEY`` env override (required for
      24/7 detached bot processes; the ``sk-xxx`` placeholder in
      config.yaml is only good inside a Mavis session).
    * Retries transient HTTP failures with exponential backoff.
    * Tracks token spend against a daily budget.
    * Supports a deterministic mock for tests.

The default model is ``minimax/MiniMax-M3`` (the same model Mavis
itself runs on). Callers can override per-request.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - httpx is in the Mavis env
    httpx = None  # type: ignore


CONFIG_PATH = Path(
    os.environ.get("MINIMAX_CONFIG")
    or (Path.home() / ".minimax" / "config.yaml")
)


class LLMError(RuntimeError):
    """Raised when the LLM call fails permanently (after retries)."""


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMBudget:
    """Daily token budget. Defaults are conservative; raise for production."""

    daily_token_limit: int = 500_000  # ~$1-3 / day at MiniMax-M3 prices
    daily_call_limit: int = 5_000

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    tokens_used: int = 0
    calls_used: int = 0
    _day_started_at: float = field(default_factory=time.time, repr=False)

    def _maybe_rollover(self) -> None:
        # Roll the counter every 24h.
        if time.time() - self._day_started_at > 86_400:
            self.tokens_used = 0
            self.calls_used = 0
            self._day_started_at = time.time()

    def charge(self, tokens: int) -> None:
        with self._lock:
            self._maybe_rollover()
            self.tokens_used += tokens
            self.calls_used += 1

    def exceeded(self) -> bool:
        with self._lock:
            self._maybe_rollover()
            return (
                self.tokens_used >= self.daily_token_limit
                or self.calls_used >= self.daily_call_limit
            )

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            self._maybe_rollover()
            return {
                "tokens_used": self.tokens_used,
                "tokens_limit": self.daily_token_limit,
                "calls_used": self.calls_used,
                "calls_limit": self.daily_call_limit,
            }


class LLMClient:
    """Thin wrapper around MiniMax's Anthropic-compatible endpoint.

    Example::

        client = LLMClient()
        resp = client.messages(
            model="minimax/MiniMax-M3",
            system="You are a careful options trader.",
            messages=[{"role": "user", "content": "Should I sell premium here?"}],
            max_tokens=512,
        )
        print(resp.text, resp.total_tokens)
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        config_path: str | Path | None = None,
        budget: LLMBudget | None = None,
        timeout_sec: float = 60.0,
        max_retries: int = 3,
        mock: Callable[..., LLMResponse] | None = None,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("MINIMAX_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or ""
        )
        self.base_url = (
            base_url
            or os.environ.get("MINIMAX_BASE_URL")
            or "https://agent.minimax.io/mavis/api/v1/llm/v1"
        )
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.budget = budget or LLMBudget()
        self.mock = mock  # for tests

        # Best-effort config load (used only for diagnostics + defaults).
        self._config_path = Path(config_path or CONFIG_PATH)
        self._config: dict[str, Any] = {}
        if self._config_path.exists() and not mock:
            try:
                import yaml  # type: ignore

                with open(self._config_path, encoding="utf-8") as fh:
                    self._config = yaml.safe_load(fh) or {}
            except Exception:  # pragma: no cover
                self._config = {}

        self._default_model = (self._config.get("defaultModel") or "minimax/MiniMax-M3").split("/")[-1]
        self._http = (
            httpx.Client(timeout=self.timeout_sec)
            if httpx is not None
            else None
        )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------
    def messages(
        self,
        *,
        model: str | None = None,
        system: str | None = None,
        messages: list[dict[str, str]],
        max_tokens: int = 1024,
        temperature: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> LLMResponse:
        """Call the Anthropic-compatible Messages API.

        ``model`` may be passed as ``"minimax/MiniMax-M3"`` or
        ``"MiniMax-M3"`` — the leading provider prefix is stripped.
        """
        if self.mock is not None:
            return self.mock(
                model=model or self._default_model,
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        if not self.api_key:
            raise LLMError(
                "MINIMAX_API_KEY env var is not set. "
                "Mint a key in the MiniMax dashboard and add it to .env, "
                "or pass api_key= to LLMClient()."
            )
        if self.budget.exceeded():
            raise LLMError(
                f"Daily LLM budget exhausted: {self.budget.snapshot()}"
            )
        if httpx is None or self._http is None:
            raise LLMError(
                "httpx is not installed. Install it or run inside the "
                "Mavis env where httpx is preinstalled."
            )

        model_id = self._strip_provider(model or self._default_model)
        body = self._build_body(model_id, system, messages, max_tokens, temperature)
        url = f"{self.base_url.rstrip('/')}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        if extra_headers:
            headers.update(extra_headers)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.post(url, headers=headers, json=body)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(
                        f"transient {resp.status_code}: {resp.text[:200]}"
                    )
                if resp.status_code >= 400:
                    raise LLMError(
                        f"permanent {resp.status_code}: {resp.text[:400]}"
                    )
                data = resp.json()
                text = self._extract_text(data)
                in_tok, out_tok = self._extract_tokens(data)
                self.budget.charge(in_tok + out_tok)
                return LLMResponse(
                    text=text,
                    model=model_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    raw=data,
                )
            except LLMError as exc:
                last_exc = exc
                # Permanent errors (>= 400 and not transient) bail out.
                msg = str(exc)
                if "permanent" in msg:
                    raise
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2 ** attempt, 8))
            except Exception as exc:  # network errors etc.
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2 ** attempt, 8))

        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempts: {last_exc}")

    def budget_snapshot(self) -> dict[str, int | float]:
        return self.budget.snapshot()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _strip_provider(model: str) -> str:
        if "/" in model:
            return model.split("/", 1)[1]
        return model

    @staticmethod
    def _build_body(
        model_id: str,
        system: str | None,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        # Anthropic Messages API requires system as a top-level field,
        # not a message with role "system".
        body: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        return body

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        # Anthropic Messages response shape.
        content = data.get("content") or []
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts).strip()

    @staticmethod
    def _extract_tokens(data: dict[str, Any]) -> tuple[int, int]:
        usage = data.get("usage") or {}
        return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    def close(self) -> None:
        if self._http is not None:
            self._http.close()


def make_mock_client(reply: str = "ok") -> LLMClient:
    """Build a deterministic mock client for tests."""

    def _mock(*_args: Any, **_kwargs: Any) -> LLMResponse:
        return LLMResponse(text=reply, model="mock/test", input_tokens=1, output_tokens=1)

    return LLMClient(mock=_mock)
