"""
LLM providers for memex's optional generation features (consolidation,
extraction, query rewriting, entity resolution).

Generation is OPT-IN: no LLM is required for memex's core retrieval. When a
provider is available, the consolidation loop and other generation features
activate; otherwise they gracefully no-op.

Discovery order (first match wins):
  1. Ollama at $OLLAMA_HOST or http://127.0.0.1:11434 — local, free, requires
     user to have installed Ollama and pulled at least one model.
  2. Anthropic API via $ANTHROPIC_API_KEY — opt-in cloud fallback.
  3. NoOp — generation features are silently disabled.

Provider auto-detection is a one-time check at startup; failures are logged,
not raised. memex never goes down because an LLM is unreachable.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


class LLMProvider(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
    ) -> str: ...


class NoOpLLMProvider:
    """Generation features stay dark when this is the active provider."""

    name = "noop"

    def is_available(self) -> bool:
        return False

    def generate(self, prompt: str, **kwargs: Any) -> str:  # noqa: ARG002
        raise RuntimeError("no LLM provider available — generation features disabled")


class OllamaProvider:
    """Local Ollama at $OLLAMA_HOST or http://127.0.0.1:11434.

    Auto-discovers an installed model (first one returned by /api/tags).
    Caller can override via $MEMEX_OLLAMA_MODEL.
    """

    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None):
        self.base_url = (base_url or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or os.environ.get("MEMEX_OLLAMA_MODEL")
        self._checked: bool | None = None

    def is_available(self) -> bool:
        if self._checked is not None:
            return self._checked
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=0.5)
            if r.status_code != 200:
                self._checked = False
                return False
            data = r.json()
            models = [m.get("name") for m in (data.get("models") or [])]
            if not models:
                log.info("ollama is up at %s but no models installed", self.base_url)
                self._checked = False
                return False
            if not self.model:
                self.model = models[0]
                log.info("ollama detected; auto-selected model: %s", self.model)
            self._checked = True
            return True
        except (httpx.HTTPError, OSError) as e:
            log.debug("ollama probe failed: %s", e)
            self._checked = False
            return False

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
    ) -> str:
        if not self.is_available():
            raise RuntimeError("ollama not available")
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }
        if system:
            body["system"] = system
        r = httpx.post(f"{self.base_url}/api/generate", json=body, timeout=60.0)
        r.raise_for_status()
        return (r.json().get("response") or "").strip()


class AnthropicProvider:
    """Anthropic Claude API via $ANTHROPIC_API_KEY. Strictly opt-in.

    Stays cold (returns is_available()=False) unless the env var is present —
    so OSS users without an API key never accidentally call it.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("MEMEX_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
    ) -> str:
        if not self.is_available():
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=60.0,
        )
        r.raise_for_status()
        data = r.json()
        return "".join(p.get("text", "") for p in (data.get("content") or []) if p.get("type") == "text").strip()


def build_default_llm() -> LLMProvider:
    """Select the best available provider; never raises.

    Tries Ollama first (local, free), then Anthropic (cloud, paid), then
    NoOp. A user who hasn't set anything up gets NoOp and a log line — never
    an exception.
    """
    ollama = OllamaProvider()
    if ollama.is_available():
        return ollama
    anthropic = AnthropicProvider()
    if anthropic.is_available():
        log.info("using Anthropic LLM provider (model=%s)", anthropic.model)
        return anthropic
    log.info("no LLM provider available — generation features disabled")
    return NoOpLLMProvider()
