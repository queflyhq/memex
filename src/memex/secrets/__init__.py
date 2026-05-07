"""Secrets vault — `secret://provider/name` handles backed by the OS keychain.

memex's secrets layer eliminates "rotate after every leak" pain at the AI
layer: the literal value never enters the concept graph, episodic stream,
or any LLM-readable surface. Whatever the AI sees is a handle of the form
`secret://provider/name`. Resolution to the actual value happens only at
trusted boundaries (the upstream HTTP client, the `secret get` CLI, an
explicit `secret_resolve` MCP call).

Backed by the OS keychain via `keyring`:
  - Windows: WinVaultKeyring (Credential Manager)
  - macOS:   KeychainKeyring (Keychain Services)
  - Linux:   SecretService (gnome-keyring / kwallet)

Index of `(provider, name, created_at, last_resolved_at)` triples is
maintained as a JSON file under the memex data dir so listing works
portably (the keyring API has no uniform list-all). The index NEVER
stores the value itself.
"""

from memex.secrets.redact import (
    RedactionEvent,
    RedactionPattern,
    RedactionResult,
    redact,
)
from memex.secrets.store import (
    SecretHandle,
    SecretIndex,
    SecretNotFoundError,
    SecretsStore,
    parse_handle,
)

__all__ = [
    "RedactionEvent",
    "RedactionPattern",
    "RedactionResult",
    "SecretHandle",
    "SecretIndex",
    "SecretNotFoundError",
    "SecretsStore",
    "parse_handle",
    "redact",
]
