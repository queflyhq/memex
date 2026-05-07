<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { GetEvents } from "../../wailsjs/go/main/App.js";

  let events: any[] = [];
  let loading = true;
  let error: string | null = null;
  let kindFilter = "";
  let windowHours = 0; // 0 = no time filter
  let pollHandle: number | undefined;
  let nowTick = Date.now();
  let tickHandle: number | undefined;

  const windowOptions = [
    { id: 1,        label: "Last hour" },
    { id: 24,       label: "Today" },
    { id: 24 * 7,   label: "This week" },
    { id: 24 * 30,  label: "This month" },
    { id: 0,        label: "All time" },
  ];

  const kindOptions = [
    "", "user_prompt", "tool_call", "tool_pre", "tool_post",
    "concept_added", "edge_added", "concept_deleted",
    "auto_approval", "auto_deny", "secret_redacted",
    "user_correction", "file_reindexed", "comment_added",
    "afk_enabled", "afk_disabled", "afk_expired",
  ];

  let limit = 1000;

  async function load(silent = false) {
    if (!silent) loading = true;
    error = null;
    try {
      const res = await GetEvents(kindFilter, limit);
      events = res.events ?? [];
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  function loadMore() {
    limit += 1000;
    load();
  }

  onMount(() => {
    load();
    pollHandle = window.setInterval(() => load(true), 5_000);
    tickHandle = window.setInterval(() => (nowTick = Date.now()), 30_000);
  });
  onDestroy(() => {
    if (pollHandle) clearInterval(pollHandle);
    if (tickHandle) clearInterval(tickHandle);
  });
  $: if (kindFilter !== undefined) load();

  // ---- Bucketing into Today / Yesterday / Earlier this week / Older ----

  type Bucket = { label: string; events: any[] };

  function relativeBucket(ts: string): string {
    const d = new Date(ts);
    const now = new Date(nowTick);
    const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const today = start(now);
    const yest = today - 86400_000;
    const dayMs = start(d);
    if (dayMs >= today) return "Today";
    if (dayMs >= yest) return "Yesterday";
    if (dayMs >= today - 6 * 86400_000) {
      return d.toLocaleDateString(undefined, { weekday: "long" });
    }
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function relTime(ts: string): string {
    const d = new Date(ts).getTime();
    const diff = Math.floor((nowTick - d) / 1000);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return new Date(ts).toLocaleString();
  }

  function eventColor(kind: string): string {
    if (kind === "auto_deny") return "#dc2626";
    if (kind === "auto_approval") return "#16a34a";
    if (kind === "secret_redacted") return "#b45309";
    if (kind === "user_correction") return "#7c3aed";
    if (kind === "file_reindexed") return "#0ea5e9";
    if (kind === "comment_added") return "#fbbf24";
    if (kind === "afk_enabled") return "#16a34a";
    if (kind === "afk_disabled" || kind === "afk_expired") return "#737373";
    if (kind === "user_prompt") return "#7c3aed";
    if (kind === "tool_call" || kind === "tool_pre" || kind === "tool_post")
      return "#0891b2";
    if (kind === "concept_added") return "#94a3b8";
    if (kind === "edge_added") return "#94a3b8";
    if (kind === "concept_deleted") return "#dc2626";
    return "#6b7280";
  }

  // Apply client-side window filter (cheaper than re-fetching). Daemon
  // returned the most recent N events; we just clip by timestamp.
  $: filteredEvents = (() => {
    if (!windowHours || windowHours <= 0) return events;
    const cutoff = nowTick - windowHours * 3600_000;
    return events.filter((e: any) => new Date(e.timestamp).getTime() >= cutoff);
  })();

  // Group consecutive identical events ("auto_approval × 12 in 4 minutes").
  // First we bucket by relative day, then within each bucket we collapse runs.
  $: buckets = (() => {
    const groups: Bucket[] = [];
    for (const ev of filteredEvents) {
      const label = relativeBucket(ev.timestamp);
      let g = groups[groups.length - 1];
      if (!g || g.label !== label) {
        g = { label, events: [] };
        groups.push(g);
      }
      g.events.push(ev);
    }
    return groups;
  })();

  function collapseRuns(arr: any[]): any[] {
    const out: any[] = [];
    let last: any = null;
    for (const ev of arr) {
      if (last && last.kind === ev.kind && last.actor === ev.actor) {
        last._count = (last._count ?? 1) + 1;
        last._latest_ts = ev.timestamp;
      } else {
        last = { ...ev, _count: 1, _latest_ts: ev.timestamp };
        out.push(last);
      }
    }
    return out;
  }
</script>

<header>
  <div>
    <h1>Activity</h1>
    <div class="subtitle">
      Episodic event stream — every prompt, tool call, decision write,
      auto-approval, redaction, hook fire. Auto-refreshes every 5s.
    </div>
  </div>
  <div class="controls">
    <div class="window-tabs">
      {#each windowOptions as opt}
        <button
          class:active={windowHours === opt.id}
          on:click={() => (windowHours = opt.id)}
        >
          {opt.label}
        </button>
      {/each}
    </div>
    <select bind:value={kindFilter}>
      {#each kindOptions as k}
        <option value={k}>{k || "(all kinds)"}</option>
      {/each}
    </select>
    <span class="muted small">
      {#if !loading}{filteredEvents.length} of {events.length}{/if}
    </span>
  </div>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else if events.length === 0}
  <p class="muted">No events match this filter.</p>
{:else}
  <div class="timeline">
    {#each buckets as bucket (bucket.label)}
      <div class="bucket">
        <div class="bucket-label">{bucket.label}</div>
        <div class="bucket-rail"></div>
        <div class="bucket-events">
          {#each collapseRuns(bucket.events) as ev (ev.id)}
            <div class="event">
              <span
                class="dot"
                style="background: {eventColor(ev.kind)}"
                title="{ev.kind}"
              ></span>
              <span class="ev-kind" style="color: {eventColor(ev.kind)}">
                {ev.kind}
                {#if ev._count > 1}<span class="run">× {ev._count}</span>{/if}
              </span>
              <span class="ev-actor">{ev.actor}</span>
              <span class="ev-time muted">{relTime(ev._latest_ts ?? ev.timestamp)}</span>
              {#if ev.payload && Object.keys(ev.payload).length}
                <details class="payload">
                  <summary>payload</summary>
                  <pre>{JSON.stringify(ev.payload, null, 2)}</pre>
                </details>
              {/if}
            </div>
          {/each}
        </div>
      </div>
    {/each}
    {#if events.length >= limit}
      <div class="load-more">
        <button on:click={loadMore}>load more (+1000)</button>
        <span class="muted small">showing {events.length} of {events.length}+ — daemon caps each call</span>
      </div>
    {/if}
  </div>
{/if}

<style>
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 24px;
    margin-bottom: 18px;
  }
  h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
    color: #1f2328;
    font-family: "Plus Jakarta Sans", sans-serif;
    letter-spacing: -0.02em;
  }
  .subtitle {
    margin-top: 4px;
    font-size: 12px;
    color: #6b7280;
    max-width: 580px;
    line-height: 1.4;
  }
  .controls {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }
  .window-tabs {
    display: flex;
    gap: 3px;
  }
  .window-tabs button {
    background: #ffffff;
    color: #57606a;
    border: 1px solid #d0d7de;
    padding: 4px 10px;
    border-radius: 5px;
    font-size: 11px;
    cursor: pointer;
    font-family: inherit;
  }
  .window-tabs button:hover {
    background: #f3f4f6;
  }
  .window-tabs button.active {
    background: #fef3c7;
    color: #1f2328;
    border-color: #fde047;
    font-weight: 600;
  }
  select {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    padding: 5px 10px;
    border-radius: 5px;
    font-size: 12px;
    font-family: inherit;
  }

  .timeline {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .bucket {
    display: grid;
    grid-template-columns: 140px 16px 1fr;
    gap: 8px;
    align-items: flex-start;
  }
  .bucket-label {
    font-size: 12px;
    font-weight: 700;
    color: #1f2328;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding-top: 4px;
    text-align: right;
    padding-right: 8px;
  }
  .bucket-rail {
    width: 2px;
    background: #e6e8eb;
    justify-self: center;
    align-self: stretch;
    margin-top: 8px;
    margin-bottom: 8px;
    border-radius: 1px;
  }
  .bucket-events {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .event {
    display: grid;
    grid-template-columns: 12px 220px 100px 90px 1fr;
    align-items: center;
    gap: 10px;
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
  }
  .ev-kind {
    font-weight: 600;
    font-family: "Fira Code", monospace;
    font-size: 11px;
  }
  .run {
    background: #fef3c7;
    color: #b45309;
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 9px;
    font-weight: 700;
    margin-left: 4px;
  }
  .ev-actor {
    color: #1f2328;
    font-family: "Fira Code", monospace;
    font-size: 11px;
  }
  .ev-time {
    color: #6b7280;
    font-size: 11px;
  }
  .payload summary {
    color: #6b7280;
    cursor: pointer;
    font-size: 11px;
  }
  .payload pre {
    margin-top: 4px;
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 4px;
    padding: 6px 8px;
    font-family: "Fira Code", monospace;
    font-size: 10px;
    color: #1f2328;
    white-space: pre-wrap;
    overflow-x: auto;
  }
  .small {
    font-size: 11px;
  }
  .load-more {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 12px;
    margin: 16px 0;
  }
  .load-more button {
    background: #fef3c7;
    border: 1px solid #fde047;
    color: #1f2328;
    padding: 6px 16px;
    border-radius: 5px;
    font-size: 12px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
  }
  .load-more button:hover {
    background: #fde047;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }
</style>
