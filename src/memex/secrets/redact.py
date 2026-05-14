"""Auto-redaction — scan inputs for known secret patterns and replace
with `secret://` handles before persisting.

The hook fires at write time (engine.add / engine.observe) so the
literal value never touches the concept graph or episodic stream.
Detected secrets are stored in the OS keychain under an auto-generated
provider/name and the original text gets the value swapped for the
handle URI.

Pattern coverage (slice 1, conservative):
  - AWS access key ID (AKIA…)
  - OpenAI API key (sk-…)
  - Anthropic API key (sk-ant-…)
  - GitHub PAT (ghp_…, gho_…, ghs_…, ghu_…, ghr_…)
  - Slack token (xox[abp]-…)
  - JWT (eyJ-prefixed three-segment base64)
  - Bearer header value (Bearer <hex/base64>)
  - PEM private key block

Patterns that are false-positive prone (`password=`, generic 32-hex)
are deliberately excluded. Users opt into stricter redaction via
`add_redaction_pattern` (programmatic) or by extending the catalog.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memex.secrets.store import SecretsStore

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RedactionPattern:
    """One named regex with its target provider name in the keychain."""
    name: str             # short label: "openai-api-key"
    provider: str         # keychain provider namespace: "openai"
    pattern: re.Pattern[str]
    severity: str = "high"


def _r(name: str, provider: str, regex: str, *, flags: int = 0,
       severity: str = "high") -> RedactionPattern:
    return RedactionPattern(
        name=name, provider=provider,
        pattern=re.compile(regex, flags),
        severity=severity,
    )


# Order matters: more-specific patterns first so generic ones don't
# pre-empt specific provider matches.
_DEFAULT_PATTERNS: tuple[RedactionPattern, ...] = (
    _r("anthropic-api-key", "anthropic", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    _r("openai-api-key", "openai", r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    _r("aws-access-key-id", "aws", r"\bAKIA[0-9A-Z]{16}\b"),
    _r("aws-temp-access-key-id", "aws", r"\bASIA[0-9A-Z]{16}\b"),
    _r("github-pat", "github",
       r"\bgh[pousr]_[A-Za-z0-9]{36,251}\b"),
    _r("slack-token", "slack",
       r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    _r("stripe-key", "stripe", r"\b(sk|rk)_(live|test)_[A-Za-z0-9]{20,}\b"),
    _r("jwt", "jwt",
       r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    _r("bearer-token", "bearer",
       r"(?:[Bb]earer|[Bb]asic)\s+[A-Za-z0-9._~+/=-]{20,}", severity="medium"),
    _r("pem-private-key", "pem",
       r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
)


@dataclass(slots=True)
class RedactionEvent:
    """One redacted match — used to log + audit."""
    pattern_name: str
    provider: str
    name: str   # the auto-generated keychain name (a hash of the value)
    handle: str
    severity: str
    span: tuple[int, int]


@dataclass(slots=True)
class RedactionResult:
    """Outcome of a redact() call."""
    redacted_text: str
    events: list[RedactionEvent] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.events)


def _name_from_value(value: str) -> str:
    """Stable, non-reversible name for a secret. SHA-256 of the value's
    first 256 chars, hex-encoded, first 12 chars. Same secret = same
    keychain entry; different secrets = different entries."""
    h = hashlib.sha256(value[:256].encode("utf-8", errors="replace")).hexdigest()
    return f"auto-{h[:12]}"


def redact(
    text: str,
    store: SecretsStore,
    *,
    extra_patterns: list[RedactionPattern] | None = None,
) -> RedactionResult:
    """Scan `text` for secret patterns. For each match, store the value
    in the keychain (under an auto-generated provider/name) and replace
    with the corresponding `secret://` handle.

    Idempotent: redacting an already-redacted string is a no-op (handles
    don't match any of the patterns).
    """
    if not text:
        return RedactionResult(redacted_text=text)

    patterns = list(_DEFAULT_PATTERNS) + list(extra_patterns or [])
    out = text
    events: list[RedactionEvent] = []

    # Apply each pattern in turn; subsequent patterns operate on the
    # already-redacted output. Track spans relative to the OUTPUT for
    # logging.
    for pat in patterns:
        new_chunks: list[str] = []
        last = 0
        for m in pat.pattern.finditer(out):
            value = m.group(0)
            name = _name_from_value(value)
            try:
                store.put(pat.provider, name, value, metadata={
                    "auto_redacted": True,
                    "pattern": pat.name,
                    "severity": pat.severity,
                })
            except Exception as e:  # noqa: BLE001
                # Loud, not silent — a redaction failure means we'd ELSE
                # leak the value. Fall back to a `***REDACTION-FAILED***`
                # placeholder so the value never reaches the concept
                # graph either way.
                log.error("auto-redaction store.put failed for %s: %s — replacing with placeholder",
                          pat.name, e)
                handle_str = f"***redaction-failed:{pat.name}***"
            else:
                handle_str = f"secret://{pat.provider}/{name}"
                events.append(RedactionEvent(
                    pattern_name=pat.name,
                    provider=pat.provider,
                    name=name,
                    handle=handle_str,
                    severity=pat.severity,
                    span=(m.start(), m.end()),
                ))
            new_chunks.append(out[last:m.start()])
            new_chunks.append(handle_str)
            last = m.end()
        if last:  # there were matches
            new_chunks.append(out[last:])
            out = "".join(new_chunks)

    return RedactionResult(redacted_text=out, events=events)
