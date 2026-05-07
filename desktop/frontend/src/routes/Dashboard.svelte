<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { GetHooksStatus } from "../../wailsjs/go/main/App.js";

  export let GetStats: (windowHours: number) => Promise<any>;

  let loading = true;
  let error: string | null = null;
  let stats: any = null;
  let hooks: any = null;
  let windowHours = 24 * 7; // default: this week
  let lastRefreshed = 0;
  let nowTick = Date.now();
  let pollHandle: number | undefined;
  let tickHandle: number | undefined;

  const POLL_INTERVAL_MS = 10_000; // auto-refresh every 10s

  const windowOptions = [
    { id: 1,        label: "Last hour" },
    { id: 24,       label: "Today" },
    { id: 24 * 7,   label: "This week" },
    { id: 24 * 30,  label: "This month" },
    { id: 0,        label: "All time" },
  ];

  async function load(hours: number, silent = false) {
    if (!silent) loading = true;
    error = null;
    try {
      stats = await GetStats(hours);
      lastRefreshed = Date.now();
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    load(windowHours);
    GetHooksStatus().then((h) => (hooks = h)).catch(() => {});
    pollHandle = window.setInterval(
      () => load(windowHours, true), POLL_INTERVAL_MS,
    );
    tickHandle = window.setInterval(() => (nowTick = Date.now()), 1000);
  });
  onDestroy(() => {
    if (pollHandle) clearInterval(pollHandle);
    if (tickHandle) clearInterval(tickHandle);
  });

  $: if (windowHours !== undefined) load(windowHours);

  $: refreshedAgo = lastRefreshed
    ? Math.max(0, Math.floor((nowTick - lastRefreshed) / 1000))
    : null;

  // ----- derived metrics -----
  $: imp = stats?.impact ?? {};
  $: prevented = (imp.auto_denies ?? 0) + (imp.secrets_redacted ?? 0);
  $: helped = (imp.auto_approvals ?? 0) + (imp.tool_calls_observed ?? 0);
  $: learned = (imp.user_corrections ?? 0);
  $: anyImpact = prevented + helped + learned + (imp.files_reindexed ?? 0) > 0;

  $: byActor = stats?.by_actor ?? {};
  $: actorRows = Object.entries(byActor as Record<string, any>)
    .map(([actor, m]) => ({
      actor,
      total: (m as any)?.events_total ?? 0,
      m,
    }))
    .sort((a, b) => b.total - a.total);

  // ----- formatting helpers -----
  function fmt(n: number | undefined): string {
    if (n == null) return "—";
    return n.toLocaleString();
  }
  function maxv(m: Record<string, number> | undefined): number {
    if (!m) return 1;
    return Math.max(1, ...Object.values(m));
  }
  function fmtKindMap(m: Record<string, number> | undefined): [string, number][] {
    if (!m) return [];
    return Object.entries(m).sort((a, b) => b[1] - a[1]);
  }
  function eventTime(s: string): string {
    if (!s) return "";
    return s.slice(11, 19);
  }
  function eventColor(kind: string): string {
    if (kind === "auto_deny" || kind.includes("denied")) return "#dc2626";
    if (kind === "auto_approval" || kind === "afk_enabled") return "#16a34a";
    if (kind === "secret_redacted") return "#b45309";
    if (kind === "user_correction") return "#7c3aed";
    return "#57606a";
  }

  // Hooks status: read from ~/.claude/settings.json, not inferred from
  // event counts (which fail for "just installed, no events yet").
  $: hooksInstalled = !!hooks?.installed;
  $: noEventsYet = !loading && stats &&
    (imp.user_prompts ?? 0) === 0 &&
    (imp.tool_calls_observed ?? 0) === 0;
  $: hooksMissingBanner = hooks !== null && !hooksInstalled;
  $: hooksWaitingBanner = hooksInstalled && noEventsYet;
</script>

<header>
  <div>
    <h1>Dashboard</h1>
    <div class="subtitle">{stats ? `${fmt(stats.concepts_total)} concepts · ${fmt(stats.vectors_total)} vectors · ${fmt(stats.events_total)} events` : "loading…"}</div>
  </div>
  <div class="header-right">
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
    {#if refreshedAgo !== null}
      <div class="refresh-meta">
        <span class="dot" class:fresh={refreshedAgo < 12}></span>
        refreshed {refreshedAgo}s ago · auto every 10s
      </div>
    {/if}
  </div>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else if !stats}
  <p class="muted">no stats payload</p>
{:else}

  {#if hooksMissingBanner}
    <div class="banner banner-warn">
      <div class="banner-icon">⚡</div>
      <div>
        <div class="banner-title">memex isn't hooked into Claude Code yet</div>
        <div class="banner-body">
          Run <code>memex hooks-install --apply</code> to start capturing
          every prompt + tool call + edit. Until then, what you see below
          is just memex's own writes (concept_added / edge_added).
        </div>
      </div>
    </div>
  {:else if hooksWaitingBanner}
    <div class="banner banner-ok">
      <div class="banner-icon">✓</div>
      <div>
        <div class="banner-title">
          Hooks installed — waiting for first event
        </div>
        <div class="banner-body">
          memex is wired into
          {#each Object.keys(hooks.events_wired ?? {}) as ev, i}
            <code>{ev}</code>{i < Object.keys(hooks.events_wired ?? {}).length - 1 ? ", " : ""}
          {/each}.
          Submit a prompt or run a tool in Claude Code; the impact tiles
          populate within a few seconds (auto-refresh every 10s).
        </div>
      </div>
    </div>
  {/if}

  {#if anyImpact}
    <section class="headline">
      <div class="headline-text">
        memex saved you
        <strong class="num-emph">{fmt(imp.auto_approvals)}</strong>
        prompt{imp.auto_approvals === 1 ? "" : "s"},
        blocked
        <strong class="num-emph">{fmt(imp.auto_denies)}</strong>
        dangerous call{imp.auto_denies === 1 ? "" : "s"}, captured
        <strong class="num-emph">{fmt(imp.user_corrections)}</strong>
        correction{imp.user_corrections === 1 ? "" : "s"}.
      </div>
    </section>
  {:else}
    <section class="headline headline-empty">
      <div class="headline-text">
        memex is recording every concept + edge written. Once Claude Code
        hooks fire user prompts and tool calls through here, this line
        becomes a real-time impact summary.
      </div>
    </section>
  {/if}

  <div class="three-col">
    <!-- Prevented -->
    <section class="card">
      <h2>What memex prevented</h2>
      <div class="metric-row">
        <div class="metric">
          <div class="metric-num red">{fmt(imp.auto_denies)}</div>
          <div class="metric-label">dangerous calls blocked</div>
          <div class="metric-hint">PreToolUse hook + hard-deny patterns</div>
        </div>
        <div class="metric">
          <div class="metric-num amber">{fmt(imp.secrets_redacted)}</div>
          <div class="metric-label">secrets redacted</div>
          <div class="metric-hint">literal API keys / tokens kept out of memory</div>
        </div>
      </div>
    </section>

    <!-- Helped -->
    <section class="card">
      <h2>What memex remembered for you</h2>
      <div class="metric-row">
        <div class="metric">
          <div class="metric-num green">{fmt(imp.auto_approvals)}</div>
          <div class="metric-label">prompts you didn't have to answer</div>
          <div class="metric-hint">auto-approve via stored policies</div>
        </div>
        <div class="metric">
          <div class="metric-num purple">{fmt(imp.user_corrections)}</div>
          <div class="metric-label">corrections captured</div>
          <div class="metric-hint">rules that survive across sessions</div>
        </div>
      </div>
    </section>

    <!-- Worked unattended -->
    <section class="card">
      <h2>What memex did unattended</h2>
      <div class="metric-row">
        <div class="metric">
          <div class="metric-num">{fmt(imp.afk_sessions)}</div>
          <div class="metric-label">AFK sessions</div>
          <div class="metric-hint">delegations memex worked through</div>
        </div>
        <div class="metric">
          <div class="metric-num">{fmt(imp.files_reindexed)}</div>
          <div class="metric-label">files re-indexed on edit</div>
          <div class="metric-hint">typed graph kept current</div>
        </div>
      </div>
    </section>
  </div>

  <h2 class="section-h">Memory at a glance</h2>
  <div class="mem-grid">
    <div class="mem-tile">
      <div class="mem-num">{fmt(stats.concepts_total)}</div>
      <div class="mem-label">concepts</div>
      <div class="mem-hint">typed graph nodes</div>
    </div>
    <div class="mem-tile">
      <div class="mem-num">{fmt(stats.edges_total)}</div>
      <div class="mem-label">edges</div>
      <div class="mem-hint">typed relations</div>
    </div>
    <div class="mem-tile">
      <div class="mem-num">{fmt(stats.events_total)}</div>
      <div class="mem-label">events in DB</div>
      <div class="mem-hint">{fmt(stats.events_in_window)} in this window</div>
    </div>
    <div class="mem-tile">
      <div class="mem-num">{fmt(stats.vectors_total)}</div>
      <div class="mem-label">vectors</div>
      <div class="mem-hint">{stats.vector_dim ?? 384}-dim · {stats.embed_model ?? "—"}</div>
    </div>
  </div>

  {#if stats.concepts_by_kind && Object.keys(stats.concepts_by_kind).length}
    <h2 class="section-h">Concepts by kind</h2>
    <div class="bars">
      {#each fmtKindMap(stats.concepts_by_kind) as [k, v]}
        <div class="bar-row">
          <span class="bar-label">{k}</span>
          <span class="bar-track">
            <span class="bar-fill" style="width: {(v / maxv(stats.concepts_by_kind)) * 100}%"></span>
          </span>
          <span class="bar-value">{fmt(v)}</span>
        </div>
      {/each}
    </div>
  {/if}

  {#if actorRows.length}
    <h2 class="section-h">Which AI tools used memex</h2>
    <div class="actor-list">
      {#each actorRows as ar}
        <div class="actor-row">
          <span class="actor-name">{ar.actor}</span>
          <span class="actor-bar">
            <span class="actor-fill" style="width: {(ar.total / maxv(Object.fromEntries(actorRows.map(r => [r.actor, r.total])))) * 100}%"></span>
          </span>
          <span class="actor-num">{fmt(ar.total)}</span>
        </div>
      {/each}
    </div>
  {/if}

  {#if stats.recent_events && stats.recent_events.length}
    <h2 class="section-h">Recent activity</h2>
    <div class="event-list">
      {#each stats.recent_events as e (e.timestamp + ":" + e.kind)}
        <div class="event">
          <span class="ev-time">{eventTime(e.timestamp ?? "")}</span>
          <span class="ev-kind" style="color: {eventColor(e.kind)}">{e.kind}</span>
          <span class="ev-actor">{e.actor}</span>
        </div>
      {/each}
    </div>
  {/if}
{/if}

<style>
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    margin-bottom: 22px;
  }
  h1 {
    margin: 0;
    font-size: 26px;
    font-weight: 700;
    color: #1f2328;
    font-family: "Plus Jakarta Sans", -apple-system, sans-serif;
    letter-spacing: -0.02em;
    line-height: 1;
  }
  .subtitle {
    margin-top: 4px;
    font-size: 12px;
    color: #6b7280;
  }
  h2 {
    margin: 0 0 12px;
    font-size: 11px;
    font-weight: 700;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.6px;
  }
  .section-h {
    margin: 24px 0 10px;
  }
  .header-right {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 6px;
  }
  .refresh-meta {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: #6b7280;
  }
  .refresh-meta .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #9ca3af;
  }
  .refresh-meta .dot.fresh {
    background: #16a34a;
  }
  .window-tabs {
    display: flex;
    gap: 4px;
  }
  .window-tabs button {
    background: #ffffff;
    border: 1px solid #d0d7de;
    color: #57606a;
    padding: 5px 11px;
    font-size: 12px;
    border-radius: 5px;
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

  .banner {
    display: grid;
    grid-template-columns: 36px 1fr;
    gap: 14px;
    padding: 14px 18px;
    border-radius: 8px;
    margin-bottom: 20px;
    align-items: flex-start;
  }
  .banner-warn {
    background: #fef9c3;
    border: 1px solid #fde047;
    color: #1f2328;
  }
  .banner-ok {
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    color: #1f2328;
  }
  .banner-icon {
    font-size: 22px;
    line-height: 1;
  }
  .banner-title {
    font-weight: 700;
    font-size: 14px;
    margin-bottom: 2px;
    font-family: "Plus Jakarta Sans", sans-serif;
  }
  .banner-body {
    font-size: 12px;
    color: #4b5563;
    line-height: 1.5;
  }
  .banner-body code {
    background: #ffffff;
    padding: 1px 6px;
    border-radius: 3px;
    font-family: "Fira Code", monospace;
    font-size: 11px;
    border: 1px solid #fde047;
  }

  .headline {
    background: #ffffff;
    border: 1px solid #fde047;
    border-left: 4px solid #fbbf24;
    border-radius: 6px;
    padding: 18px 22px;
    margin-bottom: 22px;
  }
  .headline-empty {
    border-left-color: #cbd5e1;
    border-color: #e6e8eb;
  }
  .headline-text {
    font-size: 16px;
    font-weight: 500;
    color: #1f2328;
    line-height: 1.45;
  }
  .num-emph {
    font-family: "Plus Jakarta Sans", sans-serif;
    font-weight: 800;
    font-size: 22px;
    color: #b45309;
    letter-spacing: -0.01em;
  }
  .headline-empty .num-emph,
  .headline-empty .headline-text {
    color: #4b5563;
  }

  .three-col {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-bottom: 4px;
  }
  .card {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 14px 16px;
  }
  .metric-row {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .metric {
    display: flex;
    flex-direction: column;
  }
  .metric-num {
    font-family: "Plus Jakarta Sans", sans-serif;
    font-size: 28px;
    font-weight: 800;
    line-height: 1;
    letter-spacing: -0.02em;
    color: #1f2328;
  }
  .metric-num.red { color: #dc2626; }
  .metric-num.amber { color: #b45309; }
  .metric-num.green { color: #16a34a; }
  .metric-num.purple { color: #7c3aed; }
  .metric-label {
    font-size: 12px;
    color: #1f2328;
    margin-top: 4px;
    font-weight: 500;
  }
  .metric-hint {
    font-size: 11px;
    color: #6b7280;
    margin-top: 1px;
    line-height: 1.4;
  }

  .mem-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
  }
  .mem-tile {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 12px 14px;
  }
  .mem-num {
    font-family: "Plus Jakarta Sans", sans-serif;
    font-size: 20px;
    font-weight: 700;
    color: #1f2328;
    letter-spacing: -0.02em;
  }
  .mem-label {
    font-size: 12px;
    color: #1f2328;
    font-weight: 500;
    margin-top: 2px;
  }
  .mem-hint {
    font-size: 11px;
    color: #6b7280;
    margin-top: 1px;
  }

  .bars {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .bar-row {
    display: grid;
    grid-template-columns: 140px 1fr 60px;
    align-items: center;
    gap: 12px;
    font-size: 12px;
  }
  .bar-label {
    color: #1f2328;
  }
  .bar-track {
    background: #f3f4f6;
    height: 8px;
    border-radius: 4px;
    overflow: hidden;
  }
  .bar-fill {
    background: #fbbf24;
    height: 100%;
    display: block;
  }
  .bar-value {
    color: #6b7280;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .actor-list {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .actor-row {
    display: grid;
    grid-template-columns: 140px 1fr 60px;
    align-items: center;
    gap: 12px;
    font-size: 12px;
  }
  .actor-name {
    color: #1f2328;
    font-weight: 500;
  }
  .actor-bar {
    background: #f3f4f6;
    height: 8px;
    border-radius: 4px;
    overflow: hidden;
  }
  .actor-fill {
    background: #1f2328;
    height: 100%;
    display: block;
  }
  .actor-num {
    color: #6b7280;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .event-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    font-family: "Fira Code", "Consolas", monospace;
  }
  .event {
    display: grid;
    grid-template-columns: 80px 220px 1fr;
    color: #57606a;
    padding: 4px 6px;
    border-bottom: 1px solid #f3f4f6;
  }
  .ev-time {
    color: #6b7280;
  }
  .ev-actor {
    color: #1f2328;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }
</style>
