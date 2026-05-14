"""Background maintenance scheduler.

Memex's lifecycle features (consolidation, cleanup, episodic rollup) live
in the engine but were CLI-only — nobody runs them. Without scheduled
upkeep:

  - Stale facts pile up (decay never gets pruned)
  - Co-occurrence patterns never get promoted to relates_to edges
  - Episodic events accumulate forever (73K and counting)
  - User has to remember to type `memex consolidate`

This scheduler runs them automatically on cadences that match the
typical "AI session" pattern. Stops cleanly on daemon shutdown.

Design choices:
  - Pure stdlib (threading.Timer) — no APScheduler dep
  - Each job wrapped in try/except so one failure doesn't kill the rest
  - All jobs idempotent — restart-safe
  - First-run jitter so a daemon restart doesn't trigger all jobs at once
  - Disable via MEMEX_NO_AUTO_MAINT=1 (tests, CI, debugging)
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from memex.core.engine import Engine

log = logging.getLogger(__name__)


class MaintenanceScheduler:
    """Runs engine maintenance jobs at fixed intervals on a daemon thread."""

    def __init__(self, engine: "Engine"):
        self.engine = engine
        self._timers: list[threading.Timer] = []
        self._stopped = False
        # Cadences (seconds). Tuned so a user with light traffic sees
        # roughly one job per coffee break, and heavy traffic still
        # gets sub-day freshness on patterns.
        self.consolidate_every = int(os.environ.get("MEMEX_CONSOLIDATE_EVERY_SEC", "1800"))    # 30 min
        self.episodic_rollup_every = int(os.environ.get("MEMEX_ROLLUP_EVERY_SEC", "43200"))    # 12 h
        self.cleanup_every = int(os.environ.get("MEMEX_CLEANUP_EVERY_SEC", "86400"))           # 24 h
        self.change_link_every = int(os.environ.get("MEMEX_CHANGE_LINK_EVERY_SEC", "300"))     # 5 min
        self.prompt_extract_every = int(os.environ.get("MEMEX_PROMPT_EXTRACT_EVERY_SEC", "600"))  # 10 min
        self.weekly_digest_every = int(os.environ.get("MEMEX_WEEKLY_DIGEST_EVERY_SEC", "604800"))  # 7 days
        self.verification_every = int(os.environ.get("MEMEX_VERIFICATION_EVERY_SEC", "21600"))    # 6 h
        self.team_sync_every = int(os.environ.get("MEMEX_TEAM_SYNC_EVERY_SEC", "600"))            # 10 min
        # Last-run times for the System page UI. Populated on each tick.
        self.last_run: dict[str, float] = {}

    def start(self) -> None:
        if os.environ.get("MEMEX_NO_AUTO_MAINT") == "1":
            log.info("auto-maintenance disabled by MEMEX_NO_AUTO_MAINT=1")
            return
        # First runs are jittered 30-120s after boot so we don't compete
        # with embed warmup or the user's first query.
        self._schedule(self._run_consolidate, delay=random.uniform(60, 120), every=self.consolidate_every)
        self._schedule(self._run_rollup,      delay=random.uniform(120, 180), every=self.episodic_rollup_every)
        self._schedule(self._run_cleanup,     delay=random.uniform(300, 600), every=self.cleanup_every)
        self._schedule(self._run_change_link, delay=random.uniform(60, 90),   every=self.change_link_every)
        self._schedule(self._run_prompt_facts, delay=random.uniform(90, 150), every=self.prompt_extract_every)
        # Weekly digest — first run within an hour of daemon start so a
        # fresh install gets one quickly; then every 7 days.
        self._schedule(self._run_weekly_digest, delay=random.uniform(300, 1800), every=self.weekly_digest_every)
        self._schedule(self._run_verification, delay=random.uniform(180, 360), every=self.verification_every)
        # Team git-sync only schedules when MEMEX_TEAM_REPO is set.
        if os.environ.get("MEMEX_TEAM_REPO"):
            self._schedule(self._run_team_sync, delay=random.uniform(120, 240), every=self.team_sync_every)
            log.info("team-sync enabled: every %ss against %s",
                     self.team_sync_every, os.environ.get("MEMEX_TEAM_REPO"))
        log.info(
            "auto-maintenance scheduled: consolidate=%ss rollup=%ss cleanup=%ss link=%ss prompts=%ss digest=%ss verify=%ss",
            self.consolidate_every, self.episodic_rollup_every, self.cleanup_every,
            self.change_link_every, self.prompt_extract_every, self.weekly_digest_every,
            self.verification_every,
        )

    def stop(self) -> None:
        self._stopped = True
        for t in self._timers:
            t.cancel()
        self._timers.clear()

    # ---- internals ----

    def _schedule(self, fn: Callable[[], None], *, delay: float, every: int) -> None:
        if self._stopped:
            return
        def _wrapped() -> None:
            if self._stopped:
                return
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log.warning("auto-maintenance job %s failed: %s", fn.__name__, e)
            finally:
                if not self._stopped:
                    self._schedule(fn, delay=every, every=every)
        t = threading.Timer(delay, _wrapped)
        t.daemon = True
        t.name = f"maint-{fn.__name__}"
        t.start()
        self._timers.append(t)

    def _run_consolidate(self) -> None:
        self.last_run["consolidate"] = time.time()
        t0 = time.time()
        report = self.engine.consolidate()
        log.info("auto-consolidate: %s (%.1fs)", report, time.time() - t0)

    def _run_rollup(self) -> None:
        self.last_run["rollup"] = time.time()
        try:
            from memex.core.lifecycle.episodic_rollup import run_episodic_rollup
            t0 = time.time()
            report = run_episodic_rollup(self.engine)
            log.info("auto-rollup: %s (%.1fs)", report, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            log.warning("episodic_rollup not runnable: %s", e)

    def _run_cleanup(self) -> None:
        self.last_run["cleanup"] = time.time()
        t0 = time.time()
        report = self.engine.run_cleanup()
        log.info("auto-cleanup: %s (%.1fs)", report, time.time() - t0)

    def _run_change_link(self) -> None:
        self.last_run["change_link"] = time.time()
        from memex.core.lifecycle.change_provenance import link_recent_changes
        report = link_recent_changes(self.engine)
        if report.get("links_added", 0) > 0:
            log.info("auto-change-link: %s", report)

    def _run_prompt_facts(self) -> None:
        self.last_run["prompt_facts"] = time.time()
        from memex.core.lifecycle.prompt_facts import extract_prompt_facts
        report = extract_prompt_facts(self.engine)
        if report.get("facts_added", 0) > 0:
            log.info("auto-prompt-facts: %s", report)

    def _run_weekly_digest(self) -> None:
        self.last_run["weekly_digest"] = time.time()
        from memex.core.lifecycle.weekly_digest import run_weekly_digest
        report = run_weekly_digest(self.engine)
        log.info("auto-weekly-digest: %s", report)

    def _run_verification(self) -> None:
        """Re-check falsifiable claims against ground truth."""
        from memex.core.lifecycle.verification import run_verification
        self.last_run["verification"] = time.time()
        report = run_verification(self.engine)
        if report.get("confirmed", 0) + report.get("refuted", 0) > 0:
            log.info("auto-verification: %s", report)

    def _run_team_sync(self) -> None:
        """Push local durable graph to the team repo, pull inbound. Only
        runs when MEMEX_TEAM_REPO is set in the environment (checked at
        scheduler boot)."""
        repo = os.environ.get("MEMEX_TEAM_REPO")
        if not repo:
            return
        self.last_run["team_sync"] = time.time()
        from memex.team import sync_push, sync_pull
        # Pull first so we have everyone else's latest before we push.
        pull_report = sync_pull(self.engine, repo)
        push_report = sync_push(self.engine, repo)
        log.info(
            "team-sync: pulled=%d concepts/%d edges  pushed=%d concepts/%d edges  conflicts=%d",
            pull_report.pulled_concepts, pull_report.pulled_edges,
            push_report.pushed_concepts, push_report.pushed_edges,
            pull_report.conflicts_resolved,
        )
