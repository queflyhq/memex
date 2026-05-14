"""
Time-decay for confidence.

Simple half-life model: a memory's confidence decays exponentially with time
since `last_confirmed_at`. Memories re-confirmed (by user, agent observation,
or verification pass) reset the decay clock.

The decay is computed *on read*, not via a background pass — this keeps v0.1
simple and avoids running a job to mutate the store.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from memex.core.schema import NodeKind

DEFAULT_HALF_LIFE_DAYS = 90.0


def decayed_confidence(
    base_confidence: float,
    last_confirmed_at: datetime,
    now: datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Apply exponential decay since `last_confirmed_at`.

    >>> decayed_confidence(1.0, datetime.now(timezone.utc))
    1.0
    """
    if base_confidence <= 0.0:
        return 0.0
    now = now or datetime.now(timezone.utc)
    if last_confirmed_at.tzinfo is None:
        last_confirmed_at = last_confirmed_at.replace(tzinfo=timezone.utc)
    delta = (now - last_confirmed_at).total_seconds()
    if delta <= 0.0:
        return base_confidence
    days = delta / 86_400.0
    factor = math.pow(0.5, days / half_life_days)
    return max(0.0, min(1.0, base_confidence * factor))


def half_life_for_kind(kind: NodeKind | str | None, settings) -> float:
    """Resolve per-kind half-life from Settings.

    Decisions and constraints age slowly (rules don't go stale at 30 days);
    opinions and approaches age fast. Falls back to the configured default
    when no per-kind override exists.
    """
    if kind is None:
        return settings.decay_half_life_default_days
    k = kind.value if isinstance(kind, NodeKind) else str(kind)
    attr = f"decay_half_life_{k}_days"
    return float(getattr(settings, attr, settings.decay_half_life_default_days))
