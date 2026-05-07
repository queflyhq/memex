<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { GetHooksStatus, GetNextActions } from "../../wailsjs/go/main/App.js";

  export let GetStats: (windowHours: number) => Promise<any>;
  let nextActions: any[] = [];

  async function loadNextActions() {
    try {
      const r = await GetNextActions(5);
      // /next-actions returns markdown text; parse a simple title list
      // out of it. (Daemon's render layer flattens to markdown lines
      // like "🔥 p1 | task-name | c_xxxxx".)
      if (typeof r === "string") {
        const lines = (r as string).split("\n").filter((l: string) => l.trim().startsWith("-") || l.trim().match(/^(🔥|⏳|🚧|⛔|✓)/));
        nextActions = lines.slice(0, 5).map((l: string) => ({ raw: l }));
      } else if (Array.isArray(r)) {
        nextActions = (r as any[]).slice(0, 5);
      } else {
        nextActions = [];
      }
    } catch {
      nextActions = [];
    }
  }

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
    loadNextActions();
    GetHooksStatus().then((h) => (hooks = h)).catch(() => {});
    pollHandle = window.setInterval(() => {
      load(windowHours, true);
      loadNextActions();
    }, POLL_INTERVAL_MS);
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
  $: helped = (imp.auto_approvals ?? 0) + (imp.tool_calls_observed ?? 0) + (imp.recalls ?? 0);
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
  function fmtTokens(n: number | undefined): string {
    if (n == null || isNaN(n)) return "0";
    if (n < 1000) return String(n);
    if (n < 1_000_000) return (n / 1000).toFixed(1) + "k";
    return (n / 1_000_000).toFixed(2) + "M";
  }
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

  // Concept-kind family palette — keeps related kinds visually grouped:
  // code (symbol/file/module), knowledge (fact/decision/constraint),
  // work (task/project/milestone), people (person/agent), other.
  function kindColor(kind: string): string {
    const k = (kind || "").toLowerCase();
    if (k === "symbol" || k === "file" || k === "module" || k === "source")
      return "#1d4ed8"; // code → blue
    if (k === "fact" || k === "decision" || k === "constraint" || k === "approach" || k === "note")
      return "#7c3aed"; // knowledge → purple
    if (k === "task" || k === "project" || k === "milestone")
      return "#0d9488"; // work → teal
    if (k === "person")
      return "#b45309"; // people → amber
    return "#57606a"; // other → grey
  }

  // Actor palette — same family map. agent = the AI; claude_code = the
  // hook; skill = installed skills; human = the user.
  function actorColor(actor: string): string {
    const a = (actor || "").toLowerCase();
    if (a === "agent") return "#1d4ed8";
    if (a === "claude_code" || a === "hook") return "#0d9488";
    if (a === "skill") return "#7c3aed";
    if (a === "human") return "#b45309";
    if (a === "extractor") return "#0891b2";
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
        served
        <strong class="num-emph">{fmt(imp.recalls)}</strong>
        recall{imp.recalls === 1 ? "" : "s"},
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
          <div class="metric-num blue">{fmt(imp.recalls)}</div>
          <div class="metric-label">recalls served</div>
          <div class="metric-hint">times AI asked memex for context</div>
        </div>
        <div class="metric">
          <div class="metric-num purple">{fmt(imp.user_corrections)}</div>
          <div class="metric-label">corrections captured</div>
          <div class="metric-hint">rules that survive across sessions · auto-detected from prompts</div>
        </div>
      </div>
      <div class="metric-row" style="margin-top: 14px;">
        <div class="metric">
          <div class="metric-num green">{fmtTokens(imp.tokens_injected)}</div>
          <div class="metric-label">tokens auto-injected to Claude</div>
          <div class="metric-hint">
            context the AI got for free across {fmt(imp.context_injections)} hook fires
            — you didn't have to re-explain
          </div>
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
        <div class="metric">
          <div class="metric-num">{fmt(imp.consolidations)}</div>
          <div class="metric-label">consolidations</div>
          <div class="metric-hint">sleep-cycle summaries written</div>
        </div>
      </div>
    </section>
  </div>

  <!-- Activity & growth — surfaces every other event-kind status the user
       asked about (recalls, writes, skill validations, policy changes). -->
  <h2 class="section-h">Activity & growth</h2>
  <div class="mem-grid">
    <div class="mem-tile">
      <div class="mem-num">{fmt(imp.concepts_added)}</div>
      <div class="mem-label">concepts added</div>
      <div class="mem-hint">typed nodes written</div>
    </div>
    <div class="mem-tile">
      <div class="mem-num">{fmt(imp.edges_added)}</div>
      <div class="mem-label">edges added</div>
      <div class="mem-hint">typed relations</div>
    </div>
    <div class="mem-tile">
      <div class="mem-num">{fmt(imp.skills_validated)}</div>
      <div class="mem-label">skills validated</div>
      <div class="mem-hint">{fmt(imp.skills_installed)} installed</div>
    </div>
    <div class="mem-tile">
      <div class="mem-num">{fmt(imp.policies_changed)}</div>
      <div class="mem-label">policies changed</div>
      <div class="mem-hint">{fmt(imp.sources_indexed)} sources indexed</div>
    </div>
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

  {#if stats.tasks_by_status && Object.keys(stats.tasks_by_status).length}
    <h2 class="section-h">Project management</h2>
    <div class="pm-grid">
      <div class="pm-tile pm-projects">
        <div class="pm-num">{fmt(stats.concepts_by_kind?.project ?? 0)}</div>
        <div class="pm-label">Projects</div>
        <div class="pm-hint">{fmt(stats.concepts_by_kind?.milestone ?? 0)} milestones</div>
      </div>
      {#each Object.entries(stats.tasks_by_status) as [status, n] (status)}
        <div class="pm-tile pm-status pm-status-{status}">
          <div class="pm-num">{fmt(Number(n))}</div>
          <div class="pm-label">{status.replace("_", " ")}</div>
          <div class="pm-hint">tasks</div>
        </div>
      {/each}
    </div>

    {#if nextActions.length}
      <h3 class="micro-h">Next actions</h3>
      <div class="next-list">
        {#each nextActions as na (na.raw ?? na.id)}
          <div class="next-row">
            <span class="next-bullet">▸</span>
            <span class="next-text">{na.raw ?? na.name ?? na.title ?? JSON.stringify(na)}</span>
          </div>
        {/each}
      </div>
    {/if}
  {/if}

  {#if stats.concepts_by_kind && Object.keys(stats.concepts_by_kind).length}
    <h2 class="section-h">Concepts by kind</h2>
    <!-- sqrt-sized chip cloud — heavy skew (6k symbols vs 1 person)
         crushes a linear bar chart. sqrt keeps the long tail readable
         while preserving rank. Tile color comes from the kind family. -->
    <div class="chip-cloud">
      {#each fmtKindMap(stats.concepts_by_kind) as [k, v]}
        {@const ratio = Math.sqrt(v) / Math.sqrt(maxv(stats.concepts_by_kind))}
        <div
          class="chip"
          style="
            font-size: {Math.max(11, 11 + ratio * 14)}px;
            background: {kindColor(k)}1A;
            border-color: {kindColor(k)}55;
            color: {kindColor(k)};
          "
          title="{k}: {fmt(v)}"
        >
          <span class="chip-name">{k}</span>
          <span class="chip-num">{fmt(v)}</span>
        </div>
      {/each}
    </div>
  {/if}

  {#if actorRows.length}
    <h2 class="section-h">Which AI tools used memex</h2>
    <!-- Single 100% stacked bar — far better than per-row bars when
         one actor (agent ~33k) dwarfs the rest. Tooltip + legend show
         the absolute counts. -->
    {@const actorTotal = actorRows.reduce((acc, r) => acc + r.total, 0)}
    <div class="stacked-bar">
      {#each actorRows as ar, i}
        {@const pct = actorTotal > 0 ? (ar.total / actorTotal) * 100 : 0}
        <div
          class="stacked-seg"
          style="width: {pct}%; background: {actorColor(ar.actor)};"
          title="{ar.actor}: {fmt(ar.total)} ({pct.toFixed(1)}%)"
        >
          {#if pct > 6}<span class="seg-label">{ar.actor}</span>{/if}
        </div>
      {/each}
    </div>
    <div class="stacked-legend">
      {#each actorRows as ar}
        <div class="legend-item">
          <span class="legend-dot" style="background: {actorColor(ar.actor)}"></span>
          <span class="legend-name">{ar.actor}</span>
          <span class="legend-num">{fmt(ar.total)}</span>
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
  .metric-num.blue { color: #1d4ed8; }
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
  .pm-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px;
    margin-bottom: 14px;
  }
  .pm-tile {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 12px 14px;
  }
  .pm-tile.pm-projects {
    background: #fef9c3;
    border-color: #fde047;
  }
  .pm-tile.pm-status-pending      { border-left: 3px solid #94a3b8; }
  .pm-tile.pm-status-in_progress  { border-left: 3px solid #0ea5e9; }
  .pm-tile.pm-status-blocked      { border-left: 3px solid #dc2626; }
  .pm-tile.pm-status-cancelled    { border-left: 3px solid #94a3b8; }
  .pm-tile.pm-status-completed    { border-left: 3px solid #16a34a; }
  .pm-num {
    font-family: "Plus Jakarta Sans", sans-serif;
    font-size: 22px;
    font-weight: 700;
    color: #1f2328;
  }
  .pm-label {
    font-size: 12px;
    font-weight: 500;
    color: #1f2328;
    margin-top: 2px;
    text-transform: capitalize;
  }
  .pm-hint {
    font-size: 11px;
    color: #6b7280;
    margin-top: 1px;
  }
  .micro-h {
    margin: 14px 0 6px;
    font-size: 11px;
    font-weight: 700;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .next-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .next-row {
    display: grid;
    grid-template-columns: 18px 1fr;
    gap: 8px;
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 5px;
    padding: 6px 10px;
    font-size: 12px;
  }
  .next-bullet {
    color: #fbbf24;
    font-weight: 700;
  }
  .next-text {
    color: #1f2328;
    font-family: "Fira Code", monospace;
    font-size: 11px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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

  /* sqrt-sized chip cloud — replaces the old per-row horizontal bars
     for "Concepts by kind". Family-coloured, sized so 6,295 symbols
     and 1 person both stay readable. */
  .chip-cloud {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .chip {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    padding: 6px 10px;
    border-radius: 999px;
    border: 1px solid;
    line-height: 1.1;
    font-weight: 500;
    transition: transform 60ms ease;
  }
  .chip:hover { transform: translateY(-1px); }
  .chip-name {
    text-transform: lowercase;
    letter-spacing: -0.005em;
  }
  .chip-num {
    font-variant-numeric: tabular-nums;
    font-weight: 700;
    opacity: 0.85;
    font-size: 0.9em;
  }

  /* Single 100% stacked bar for "Which AI tools used memex" — when
     one actor has 33k events and the next has 226 the per-row bar
     just becomes "one full + three invisible". The stacked share is
     the right read. */
  .stacked-bar {
    display: flex;
    width: 100%;
    height: 28px;
    border-radius: 6px;
    overflow: hidden;
    background: #f3f4f6;
  }
  .stacked-seg {
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.01em;
    overflow: hidden;
    white-space: nowrap;
    transition: filter 80ms ease;
  }
  .stacked-seg:hover { filter: brightness(1.05); }
  .seg-label {
    padding: 0 8px;
    text-shadow: 0 1px 0 rgba(0,0,0,0.15);
  }
  .stacked-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    margin-top: 10px;
    font-size: 12px;
  }
  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .legend-dot {
    width: 10px;
    height: 10px;
    border-radius: 2px;
    display: inline-block;
  }
  .legend-name { color: #1f2328; font-weight: 500; }
  .legend-num {
    color: #6b7280;
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
