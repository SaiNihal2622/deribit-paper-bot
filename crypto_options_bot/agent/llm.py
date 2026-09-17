"""LLM client — multi-provider router with config-driven auth.

The agent layer needs to call an LLM for Trader / Evolver / Reflector
decisions. We support many providers because:

1. The default MiniMax proxy (``agent.minimax.io``) only accepts
   user-session tokens, not standalone API keys — so a user-provided
   ``MINIMAX_API_KEY`` may be rejected (``auth failed``).
2. Some users have Anthropic / OpenAI / OpenRouter / Groq keys that
   work first try on those providers' real APIs.
3. Some providers (Groq, OpenRouter) have free tiers that work for
   the small decisions the agent layer makes (a few hundred tokens
   per call).

The router:

* Reads ``agent.providers`` from ``settings.yaml``. Each entry is
  ``{name, base_url, model, auth: "bearer" | "x-api-key", api_key_env,
   protocol: "messages" | "chat-completions", headers: {}}``.
* On every call, tries providers in order. The first one that returns
  200 wins. The last-known-good provider is cached so we don't hammer
  failed endpoints.
* If ``MINIMAX_API_KEY`` (or any provider's env var) changes at
  runtime, the next call automatically retries.
* On ``AuthError`` (401/403), marks that provider dead and moves on.

Example ``settings.yaml`` block::

    agent:
      providers:
        - name: minimax-proxy
          base_url: https://agent.minimax.io/mavis/api/v1/llm/v1
          model: MiniMax-M3
          protocol: messages
          auth: bearer
          api_key_env: MINIMAX_API_KEY
        - name: anthropic
          base_url: https://api.anthropic.com
          model: claude-3-5-haiku-20241022
          protocol: messages
          auth: x-api-key
          api_key_env: ANTHROPIC_API_KEY
        - name: openrouter
          base_url: https://openrouter.ai/api/v1
          model: openai/gpt-4o-mini
          protocol: chat-completions
          auth: bearer
          api_key_env: OPENROUTER_API_KEY
        - name: groq
          base_url: https://api.groq.com/openai/v1
          model: llama-3.1-8b-instant
          protocol: chat-completions
          auth: bearer
          api_key_env: GROQ_API_KEY
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore


CONFIG_PATH = Path(
    os.environ.get("MINIMAX_CONFIG") or (Path.home() / ".minimax" / "config.yaml")
)


# ---------------------------------------------------------------------------
# Errors + response shape
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Raised on any unrecoverable LLM error."""


class LLMAuthError(LLMError):
    """Raised when a provider rejects the API key (401/403)."""


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    provider: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def content(self) -> list[dict[str, str]]:
        return [{"type": "text", "text": self.text}]

    @property
    def usage(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


@dataclass
class LLMBudget:
    """Daily token budget. Defaults are conservative; raise for production."""

    daily_token_limit: int = 500_000  # ~$1-3 / day at MiniMax-M3 prices
    daily_call_limit: int = 5_000
    _spent_tokens: int = 0
    _calls: int = 0
    _date: str = ""

    def _rollover_if_new_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._date:
            self._date = today
            self._spent_tokens = 0
            self._calls = 0

    def exceeded(self) -> bool:
        self._rollover_if_new_day()
        return (
            self._spent_tokens >= self.daily_token_limit
            or self._calls >= self.daily_call_limit
        )

    def charge(self, tokens: int) -> None:
        self._rollover_if_new_day()
        self._spent_tokens += int(tokens)
        self._calls += 1

    def snapshot(self) -> dict[str, int | float]:
        self._rollover_if_new_day()
        return {
            "spent_tokens_today": self._spent_tokens,
            "calls_today": self._calls,
            "daily_token_limit": self.daily_token_limit,
            "daily_call_limit": self.daily_call_limit,
            "remaining_tokens": max(0, self.daily_token_limit - self._spent_tokens),
            "remaining_calls": max(0, self.daily_call_limit - self._calls),
        }


# ---------------------------------------------------------------------------
# Provider definition
# ---------------------------------------------------------------------------


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    protocol: str = "messages"            # "messages" | "chat-completions"
    auth: str = "bearer"                  # "bearer" | "x-api-key"
    api_key_env: str = "MINIMAX_API_KEY"  # env var holding the key
    headers: dict[str, str] = field(default_factory=dict)
    # Optional static extra headers (e.g. anthropic-version)

    def with_key(self) -> "Provider":
        p = Provider(
            name=self.name,
            base_url=self.base_url,
            model=self.model,
            protocol=self.protocol,
            auth=self.auth,
            api_key_env=self.api_key_env,
            headers=dict(self.headers),
        )
        p.api_key = os.environ.get(self.api_key_env, "").strip()
        return p

    api_key: str = ""  # populated by with_key()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


# Defaults used when ``settings.yaml`` has no ``agent.providers`` block.
# Order matters: the first provider with a valid key wins.
DEFAULT_PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "minimax-proxy",
        "base_url": "https://agent.minimax.io/mavis/api/v1/llm/v1",
        "model": "MiniMax-M3",
        "protocol": "messages",
        "auth": "bearer",
        "api_key_env": "MINIMAX_API_KEY",
    },
    {
        "name": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-3-5-haiku-20241022",
        "protocol": "messages",
        "auth": "x-api-key",
        "api_key_env": "ANTHROPIC_API_KEY",
        "headers": {"anthropic-version": "2023-06-01"},
    },
    {
        "name": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "protocol": "chat-completions",
        "auth": "bearer",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.1-8b-instant",
        "protocol": "chat-completions",
        "auth": "bearer",
        "api_key_env": "GROQ_API_KEY",
    },
    {
        "name": "mistral",
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "protocol": "chat-completions",
        "auth": "bearer",
        "api_key_env": "MISTRAL_API_KEY",
    },
]


class LLMClient:
    """Multi-provider router.

    Backwards-compatible with the old single-endpoint signature. If no
    providers are configured, falls back to ``DEFAULT_PROVIDERS``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        config_path: str | Path | None = None,
        budget: LLMBudget | None = None,
        timeout_sec: float = 60.0,
        max_retries: int = 2,
        mock: Callable[..., LLMResponse] | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.budget = budget or LLMBudget()
        self.mock = mock

        # Backwards-compat: if api_key + base_url are passed, build a
        # single inline provider for the legacy "one endpoint" case.
        self._legacy_inline: Optional[Provider] = None
        if api_key and base_url:
            self._legacy_inline = Provider(
                name="legacy-inline",
                base_url=base_url,
                model="MiniMax-M3",
                protocol="messages",
                auth="bearer",
                api_key_env="__inline__",
            ).with_key()
            self._legacy_inline.api_key = api_key

        self._config_path = Path(config_path or CONFIG_PATH)
        self._settings_path = Path(settings_path) if settings_path else None
        self._providers: list[Provider] = []
        self._last_good_provider: Optional[str] = None
        self._dead_providers: dict[str, float] = {}  # name -> expires_at
        self._last_config_mtime: float = 0.0
        self._last_settings_mtime: float = 0.0
        self._http: Optional[httpx.Client] = (
            httpx.Client(timeout=self.timeout_sec) if httpx is not None else None
        )

        self._reload_providers(force=True)

    # ------------------------------------------------------------------
    # Config / provider reload
    # ------------------------------------------------------------------
    def _reload_providers(self, force: bool = False) -> None:
        """Re-read settings.yaml + .env if either changed.

        Cheap to call every request — it's two stat()s and a yaml parse.
        """
        # .env mtime — if it changed, we may have a new API key. The env
        # var itself doesn't auto-reload; we re-read it every call from
        # ``os.environ`` which the operator updates on restart.
        if self._legacy_inline is not None:
            # Legacy single-endpoint mode; nothing to refresh.
            self._providers = [self._legacy_inline]
            return

        if self._settings_path is not None:
            try:
                mtime = self._settings_path.stat().st_mtime
                if force or mtime != self._last_settings_mtime:
                    self._last_settings_mtime = mtime
                    providers = self._load_providers_from_settings()
                    self._providers = providers
            except OSError:
                self._providers = self._default_providers()
        else:
            self._providers = self._default_providers()

        # Refresh env-backed keys every time so a .env edit followed by
        # an operator restart is picked up without reloading this object.
        for p in self._providers:
            p.api_key = os.environ.get(p.api_key_env, "").strip()

    def _default_providers(self) -> list[Provider]:
        return [
            Provider(
                name=d["name"],
                base_url=d["base_url"],
                model=d["model"],
                protocol=d.get("protocol", "messages"),
                auth=d.get("auth", "bearer"),
                api_key_env=d.get("api_key_env", "MINIMAX_API_KEY"),
                headers=d.get("headers", {}),
            )
            .with_key()
            for d in DEFAULT_PROVIDERS
        ]

    def _load_providers_from_settings(self) -> list[Provider]:
        if not self._settings_path or not self._settings_path.exists():
            return self._default_providers()
        try:
            import yaml  # type: ignore

            with open(self._settings_path, encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
        except Exception:
            return self._default_providers()

        agent_cfg = cfg.get("agent", {})
        providers_cfg = agent_cfg.get("providers")
        if not providers_cfg:
            return self._default_providers()

        out: list[Provider] = []
        for d in providers_cfg:
            try:
                p = Provider(
                    name=str(d["name"]),
                    base_url=str(d["base_url"]).rstrip("/"),
                    model=str(d["model"]),
                    protocol=str(d.get("protocol", "messages")),
                    auth=str(d.get("auth", "bearer")),
                    api_key_env=str(d.get("api_key_env", d.get("api_key_env", ""))),
                    headers=dict(d.get("headers") or {}),
                ).with_key()
            except (KeyError, TypeError):
                continue
            out.append(p)
        return out or self._default_providers()

    # ------------------------------------------------------------------
    # Surface
    # ------------------------------------------------------------------
    def provider_status(self) -> list[dict[str, Any]]:
        """Snapshot of every configured provider — useful for the dashboard."""
        self._reload_providers()
        out = []
        for p in self._providers:
            out.append(
                {
                    "name": p.name,
                    "model": p.model,
                    "base_url": p.base_url,
                    "auth": p.auth,
                    "api_key_env": p.api_key_env,
                    "key_present": bool(p.api_key),
                    "key_prefix": p.api_key[:8] + "..." if p.api_key else "(empty)",
                    "is_last_good": p.name == self._last_good_provider,
                    "is_dead_until": self._dead_providers.get(p.name, 0.0),
                    "dead_now": self._dead_providers.get(p.name, 0.0) > time.time(),
                }
            )
        return out

    def budget_snapshot(self) -> dict[str, int | float]:
        return self.budget.snapshot()

    # ------------------------------------------------------------------
    # The router
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
        provider: str | None = None,
    ) -> LLMResponse:
        """Call the LLM via the configured providers.

        ``model`` may be passed as ``"minimax/MiniMax-M3"`` or
        ``"MiniMax-M3"`` — the leading provider prefix is stripped.
        ``provider`` forces a specific provider name; otherwise we
        try in order, preferring the last known-good one first.
        """
        if self.mock is not None:
            return self.mock(
                model=model or "mock",
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        self._reload_providers()

        if not self._providers:
            raise LLMError(
                "No LLM providers configured. Add 'agent.providers' to "
                "settings.yaml or set MINIMAX_API_KEY."
            )

        if self.budget.exceeded():
            raise LLMError(
                f"Daily LLM budget exhausted: {self.budget.snapshot()}"
            )

        if httpx is None or self._http is None:
            raise LLMError("httpx is not installed.")

        order = self._build_provider_order(provider)
        last_error: Optional[Exception] = None
        for p in order:
            if not p.api_key:
                continue  # skip providers without a key
            if self._dead_providers.get(p.name, 0.0) > time.time():
                continue  # skip providers temporarily blacklisted

            try:
                resp = self._call_provider(
                    p,
                    model=model,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    extra_headers=extra_headers,
                )
                self._last_good_provider = p.name
                self._dead_providers.pop(p.name, None)
                return resp
            except LLMAuthError as exc:
                # This provider's key is wrong. Blacklist for 30 min.
                self._dead_providers[p.name] = time.time() + 1800
                last_error = exc
                continue
            except LLMError as exc:
                last_error = exc
                continue

        if last_error is None:
            raise LLMError(
                "No LLM provider could be reached. Check that at least one "
                "API key is set in your environment "
                f"(tried: {[p.name for p in order]})."
            )
        raise last_error

    def _build_provider_order(self, force: Optional[str]) -> list[Provider]:
        if force:
            for p in self._providers:
                if p.name == force:
                    return [p]
            raise LLMError(f"Unknown provider '{force}'.")

        # Last known-good first, then the rest in order.
        if self._last_good_provider:
            ordered = []
            for p in self._providers:
                if p.name == self._last_good_provider:
                    ordered.append(p)
                    break
            for p in self._providers:
                if p.name != self._last_good_provider:
                    ordered.append(p)
            return ordered
        return list(self._providers)

    # ------------------------------------------------------------------
    # Per-provider HTTP call
    # ------------------------------------------------------------------
    def _call_provider(
        self,
        provider: Provider,
        *,
        model: str | None,
        system: str | None,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
        extra_headers: dict[str, str] | None,
    ) -> LLMResponse:
        assert self._http is not None
        model_id = self._strip_provider(model or provider.model)
        url, body = self._build_request(
            provider, model_id, system, messages, max_tokens, temperature
        )
        headers = self._build_headers(provider, extra_headers)

        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.post(url, headers=headers, json=body)
            except Exception as exc:  # network errors
                if attempt >= self.max_retries:
                    raise LLMError(
                        f"[{provider.name}] network error after "
                        f"{self.max_retries + 1} attempts: {exc}"
                    ) from exc
                time.sleep(min(2 ** attempt, 8))
                continue

            if resp.status_code in (401, 403):
                raise LLMAuthError(
                    f"[{provider.name}] auth failed {resp.status_code}: {resp.text[:200]}"
                )
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt >= self.max_retries:
                    raise LLMError(
                        f"[{provider.name}] transient {resp.status_code}: {resp.text[:200]}"
                    )
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code >= 400:
                raise LLMError(
                    f"[{provider.name}] permanent {resp.status_code}: {resp.text[:400]}"
                )

            data = resp.json()
            text = self._extract_text(provider, data)
            in_tok, out_tok = self._extract_tokens(provider, data)
            self.budget.charge(in_tok + out_tok)
            return LLMResponse(
                text=text,
                model=model_id,
                input_tokens=in_tok,
                output_tokens=out_tok,
                provider=provider.name,
                raw=data,
            )

        raise LLMError(f"[{provider.name}] gave up after {self.max_retries + 1} attempts")

    @staticmethod
    def _strip_provider(model: str) -> str:
        if "/" in model:
            return model.split("/", 1)[1]
        return model

    def _build_request(
        self,
        provider: Provider,
        model_id: str,
        system: str | None,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
    ) -> tuple[str, dict[str, Any]]:
        if provider.protocol == "messages":
            url = f"{provider.base_url.rstrip('/')}/v1/messages"
            body: dict[str, Any] = {
                "model": model_id,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                body["system"] = system
            if temperature is not None:
                body["temperature"] = temperature
            return url, body

        # chat-completions (OpenAI shape)
        url = f"{provider.base_url.rstrip('/')}/chat/completions"
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        body = {"model": model_id, "max_tokens": max_tokens, "messages": msgs}
        if temperature is not None:
            body["temperature"] = temperature
        return url, body

    def _build_headers(
        self,
        provider: Provider,
        extra_headers: dict[str, str] | None,
    ) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if provider.headers:
            h.update(provider.headers)
        if provider.auth == "bearer":
            h["Authorization"] = f"Bearer {provider.api_key}"
        elif provider.auth == "x-api-key":
            h["x-api-key"] = provider.api_key
        else:
            # Custom auth header name; treat as bearer-style
            h[provider.auth] = provider.api_key
        if extra_headers:
            h.update(extra_headers)
        return h

    @staticmethod
    def _extract_text(provider: Provider, data: dict[str, Any]) -> str:
        if provider.protocol == "messages":
            content = data.get("content") or []
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "".join(parts).strip()
        # chat-completions
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def _extract_tokens(provider: Provider, data: dict[str, Any]) -> tuple[int, int]:
        if provider.protocol == "messages":
            usage = data.get("usage") or {}
            return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        usage = data.get("usage") or {}
        return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._http is not None:
            self._http.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_client(reply: str = "ok") -> LLMClient:
    """Build a deterministic mock client for tests."""

    def _mock(*_args: Any, **_kwargs: Any) -> LLMResponse:
        return LLMResponse(text=reply, model="mock/test", input_tokens=1, output_tokens=1)

    return LLMClient(mock=_mock)
