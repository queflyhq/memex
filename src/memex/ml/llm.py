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
        heavy: bool = False,
    ) -> str: ...


class NoOpLLMProvider:
    """Generation features stay dark when this is the active provider."""

    name = "noop"

    def is_available(self) -> bool:
        return False

    def generate(self, prompt: str, *, heavy: bool = False, **kwargs: Any) -> str:  # noqa: ARG002
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

    Two models: a fast/cheap default for routine work, and a heavy model
    that callers can request via `heavy=True` for synthesis tasks
    (consolidation, code regen, cognitive bundle summarisation).
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 heavy_model: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get(
            "MEMEX_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
        )
        self.heavy_model = heavy_model or os.environ.get(
            "MEMEX_ANTHROPIC_HEAVY_MODEL", "claude-sonnet-4-6"
        )

    def is_available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
        heavy: bool = False,
    ) -> str:
        if not self.is_available():
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        body: dict[str, Any] = {
            "model": self.heavy_model if heavy else self.model,
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


class OpenAIProvider:
    """OpenAI / OpenAI-compatible API via $OPENAI_API_KEY.

    Also works with any OpenAI-compatible endpoint (vLLM, LM Studio,
    LocalAI, Together, Groq, Fireworks) by setting $OPENAI_BASE_URL.

    Two models: routine + heavy, same as AnthropicProvider.
    """

    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 heavy_model: str | None = None, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("MEMEX_OPENAI_MODEL", "gpt-4o-mini")
        self.heavy_model = heavy_model or os.environ.get(
            "MEMEX_OPENAI_HEAVY_MODEL", "gpt-4o"
        )

    def is_available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
        heavy: bool = False,
    ) -> str:
        if not self.is_available():
            raise RuntimeError("OPENAI_API_KEY not set")
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": self.heavy_model if heavy else self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        r = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=60.0,
        )
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        return ((choices[0].get("message") or {}).get("content") or "").strip()


def build_default_llm() -> LLMProvider:
    """Select the active LLM provider; never raises.

    Explicit selection: $MEMEX_LLM ∈ {ollama, anthropic, openai, none}
    overrides auto-discovery — useful when you have multiple keys but
    want a specific provider for cost or compliance reasons.

    Auto-discovery order: Anthropic → OpenAI → Ollama → NoOp.
    Anthropic first because the user has stated a Claude preference;
    OpenAI second so $OPENAI_API_KEY users get a heavy model by default;
    Ollama last (local but smaller / weaker models). No LLM = NoOp.
    """
    explicit = (os.environ.get("MEMEX_LLM") or "").lower().strip()
    if explicit == "none":
        return NoOpLLMProvider()
    if explicit == "anthropic":
        p = AnthropicProvider()
        return p if p.is_available() else NoOpLLMProvider()
    if explicit == "openai":
        p = OpenAIProvider()
        return p if p.is_available() else NoOpLLMProvider()
    if explicit == "ollama":
        p = OllamaProvider()
        return p if p.is_available() else NoOpLLMProvider()
    # Auto-discovery
    anthropic = AnthropicProvider()
    if anthropic.is_available():
        log.info("using Anthropic LLM (default=%s, heavy=%s)",
                 anthropic.model, anthropic.heavy_model)
        return anthropic
    openai = OpenAIProvider()
    if openai.is_available():
        log.info("using OpenAI LLM (default=%s, heavy=%s, base=%s)",
                 openai.model, openai.heavy_model, openai.base_url)
        return openai
    ollama = OllamaProvider()
    if ollama.is_available():
        log.info("using Ollama LLM (model=%s)", ollama.model)
        return ollama
    log.info("no LLM provider available — generation features disabled "
             "(set ANTHROPIC_API_KEY, OPENAI_API_KEY, or run Ollama)")
    return NoOpLLMProvider()
