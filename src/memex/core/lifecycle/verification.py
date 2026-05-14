"""Verification pass — re-check falsifiable claims against ground truth.

Critic-hat: "Confidence is decoration. Nothing calibrates it from truth."

A concept's `verification` field is a small spec that tells memex how to
re-check the claim. Three executors ship:

  - `file_contains_regex` — assert a regex matches some text in a file.
    Example: a fact "auth uses JWT" might verify against
    `{"kind": "file_contains_regex", "file": "src/auth.py", "pattern": "jwt"}`
  - `file_exists` — assert a path exists.
  - `http_probe`   — GET a URL, assert status 2xx (and optional regex
    against body).

When a verification passes:
  - `last_confirmed_at` is bumped (recency-decay clock resets)
  - `confidence` nudges up (+0.05, capped at 1.0)

When it fails:
  - `confidence` nudges down (-0.10)
  - if confidence drops below 0.20, mark `metadata.refuted = true`
    so cleanup considers it a candidate for forgetting

Idempotent. Safe to run on a schedule. Skips concepts with no
`verification` field (most concepts won't have one).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


def run_verification(
    engine: "Engine",
    *,
    max_concepts: int = 100,
) -> dict[str, int]:
    """Walk concepts with a `verification` predicate; re-check each.

    Returns counts so the scheduler can log meaningfully.
    """
    t0 = time.time()
    confirmed = 0
    refuted = 0
    skipped = 0
    errors = 0
    try:
        with engine.semantic._lock:  # type: ignore[attr-defined]
            rows = engine.semantic.conn.execute(  # type: ignore[attr-defined]
                "SELECT id FROM concepts WHERE verification IS NOT NULL LIMIT ?",
                [max_concepts],
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("verification query failed: %s", e)
        return {"confirmed": 0, "refuted": 0, "skipped": 0, "errors": 1}

    for (cid,) in rows:
        try:
            c = engine.get(cid)
            if c is None or not c.verification:
                skipped += 1
                continue
            verdict = _check_predicate(c.verification)
            if verdict is None:
                skipped += 1
                continue
            if verdict:
                # Confirmed — bump last_confirmed_at + small confidence boost.
                c.last_confirmed_at = datetime.now(timezone.utc)
                c.confidence = min(1.0, c.confidence + 0.05)
                if (c.metadata or {}).get("refuted"):
                    # Was refuted before; now confirmed — clear the flag.
                    md = dict(c.metadata or {})
                    md.pop("refuted", None)
                    c.metadata = md
                engine.put(c)
                confirmed += 1
            else:
                c.confidence = max(0.0, c.confidence - 0.10)
                if c.confidence < 0.20:
                    md = dict(c.metadata or {})
                    md["refuted"] = True
                    md["refuted_at"] = datetime.now(timezone.utc).isoformat()
                    c.metadata = md
                engine.put(c)
                refuted += 1
        except Exception as e:  # noqa: BLE001
            log.debug("verification failed for %s: %s", cid, e)
            errors += 1
            continue

    elapsed_ms = round((time.time() - t0) * 1000.0, 1)
    log.info(
        "verification: confirmed=%d refuted=%d skipped=%d errors=%d (%.1fms)",
        confirmed, refuted, skipped, errors, elapsed_ms,
    )
    return {
        "confirmed": confirmed,
        "refuted": refuted,
        "skipped": skipped,
        "errors": errors,
        "elapsed_ms": elapsed_ms,
    }


def _check_predicate(spec: Any) -> bool | None:
    """Run one verification predicate. Returns True/False/None (skip-unknown).

    `spec` is a dict with `kind` and per-kind args, or a string shorthand:
      - "file:<path>"               → file_exists
      - "regex:<file>:<pattern>"    → file_contains_regex
      - "http:<url>"                → http_probe
    A dict allows richer args and is preferred for new predicates.
    """
    if isinstance(spec, str):
        if spec.startswith("file:"):
            return _file_exists(spec[5:])
        if spec.startswith("regex:"):
            rest = spec[6:]
            if ":" in rest:
                file_path, pattern = rest.split(":", 1)
                return _file_contains_regex(file_path, pattern)
        if spec.startswith("http:") or spec.startswith("https:"):
            return _http_probe(spec)
        return None
    if not isinstance(spec, dict):
        return None
    kind = spec.get("kind")
    if kind == "file_exists":
        return _file_exists(spec.get("path", ""))
    if kind == "file_contains_regex":
        return _file_contains_regex(spec.get("file", ""), spec.get("pattern", ""))
    if kind == "http_probe":
        return _http_probe(spec.get("url", ""), spec.get("body_pattern"))
    return None


def _file_exists(path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).exists()
    except Exception:  # noqa: BLE001
        return False


def _file_contains_regex(path: str, pattern: str) -> bool:
    if not path or not pattern:
        return False
    try:
        p = Path(path)
        if not p.is_file():
            return False
        text = p.read_text(encoding="utf-8", errors="replace")
        return bool(re.search(pattern, text, re.MULTILINE))
    except Exception:  # noqa: BLE001
        return False


def _http_probe(url: str, body_pattern: str | None = None) -> bool:
    if not url:
        return False
    try:
        import httpx
        r = httpx.get(url, timeout=3.0, follow_redirects=True)
        if not (200 <= r.status_code < 300):
            return False
        if body_pattern:
            return bool(re.search(body_pattern, r.text, re.MULTILINE))
        return True
    except Exception:  # noqa: BLE001
        return False
