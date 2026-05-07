<script lang="ts">
  import { onMount } from "svelte";

  export let GetStats: (windowHours: number) => Promise<any>;

  let loading = true;
  let error: string | null = null;
  let stats: any = null;
  let windowHours = 0; // 0 = all time

  const windowOptions = [
    { id: 0, label: "All time" },
    { id: 1, label: "Last hour" },
    { id: 24, label: "Today" },
    { id: 24 * 7, label: "This week" },
    { id: 24 * 30, label: "This month" },
  ];

  async function load(hours: number) {
    loading = true;
    error = null;
    try {
      stats = await GetStats(hours);
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  onMount(() => load(windowHours));

  $: if (windowHours !== undefined) load(windowHours);

  function tile(label: string, value: any, hint = "") {
    return { label, value, hint };
  }

  $: impactTiles = stats?.impact
    ? [
        tile("Auto-approvals", stats.impact.auto_approvals ?? 0,
             "user prompts memex saved you"),
        tile("Auto-denies", stats.impact.auto_denies ?? 0,
             "dangerous calls blocked"),
        tile("Secrets redacted", stats.impact.secrets_redacted ?? 0,
             "literal API keys kept out of memory"),
        tile("Corrections captured", stats.impact.user_corrections ?? 0,
             "rules now persistent across sessions"),
        tile("Files re-indexed", stats.impact.files_reindexed ?? 0,
             "edits where the typed graph stayed current"),
        tile("Tool calls observed", stats.impact.tool_calls_observed ?? 0,
             "gateway-proxied calls audited"),
        tile("User prompts seen", stats.impact.user_prompts ?? 0,
             "turns memex injected context into"),
        tile("AFK sessions", stats.impact.afk_sessions ?? 0,
             "delegations memex worked through"),
      ]
    : [];

  $: graphTiles = stats
    ? [
        tile("Concepts", stats.concepts_total ?? 0, "graph nodes"),
        tile("Edges", stats.edges_total ?? 0, "typed relations"),
        tile("Events", stats.events_total ?? 0, "episodic stream"),
      ]
    : [];

  function fmtKindMap(m: Record<string, number> | undefined): [string, number][] {
    if (!m) return [];
    return Object.entries(m).sort((a, b) => b[1] - a[1]);
  }
</script>

<header>
  <h1>Dashboard</h1>
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
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else if !stats}
  <p class="muted">no stats payload</p>
{:else}
  {#if stats.degraded}
    <div class="degraded">
      ⚠ degraded: {stats.degraded_reason ?? "unknown reason"}
    </div>
  {/if}

  <h2>Impact — what memex did for you</h2>
  <div class="grid">
    {#each impactTiles as t}
      <div class="tile">
        <div class="value">{t.value}</div>
        <div class="label">{t.label}</div>
        <div class="hint">{t.hint}</div>
      </div>
    {/each}
  </div>

  <h2>Graph size</h2>
  <div class="grid grid-3">
    {#each graphTiles as t}
      <div class="tile">
        <div class="value">{t.value}</div>
        <div class="label">{t.label}</div>
        <div class="hint">{t.hint}</div>
      </div>
    {/each}
  </div>

  {#if stats.concepts_by_kind}
    <h2>Concepts by kind</h2>
    <div class="bars">
      {#each fmtKindMap(stats.concepts_by_kind) as [k, v]}
        <div class="bar-row">
          <span class="bar-label">{k}</span>
          <span class="bar-track">
            <span
              class="bar-fill"
              style="width: {Math.min(100, (v / Math.max(...Object.values(stats.concepts_by_kind))) * 100)}%"
            ></span>
          </span>
          <span class="bar-value">{v}</span>
        </div>
      {/each}
    </div>
  {/if}

  {#if stats.events_by_actor}
    <h2>Events by tool / actor</h2>
    <div class="bars">
      {#each fmtKindMap(stats.events_by_actor) as [k, v]}
        <div class="bar-row">
          <span class="bar-label">{k}</span>
          <span class="bar-track">
            <span
              class="bar-fill alt"
              style="width: {Math.min(100, (v / Math.max(...Object.values(stats.events_by_actor))) * 100)}%"
            ></span>
          </span>
          <span class="bar-value">{v}</span>
        </div>
      {/each}
    </div>
  {/if}

  {#if stats.tasks_by_status && Object.keys(stats.tasks_by_status).length}
    <h2>Tasks by status</h2>
    <div class="bars">
      {#each fmtKindMap(stats.tasks_by_status) as [k, v]}
        <div class="bar-row">
          <span class="bar-label">{k}</span>
          <span class="bar-track">
            <span
              class="bar-fill"
              style="width: {Math.min(100, (v / Math.max(...Object.values(stats.tasks_by_status))) * 100)}%"
            ></span>
          </span>
          <span class="bar-value">{v}</span>
        </div>
      {/each}
    </div>
  {/if}

  {#if stats.recent_events && stats.recent_events.length}
    <h2>Recent activity</h2>
    <div class="event-list">
      {#each stats.recent_events as e}
        <div class="event">
          <span class="ev-time">{(e.timestamp ?? "").slice(11, 19)}</span>
          <span class="ev-kind">{e.kind}</span>
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
    align-items: center;
    margin-bottom: 18px;
  }
  h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
    color: #1f2328;
  }
  h2 {
    margin: 24px 0 10px;
    font-size: 12px;
    font-weight: 600;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.6px;
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
  .grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
    margin-bottom: 8px;
  }
  .grid-3 {
    grid-template-columns: repeat(3, 1fr);
  }
  .tile {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 14px 16px;
  }
  .value {
    font-size: 24px;
    font-weight: 700;
    color: #1f2328;
  }
  .label {
    font-size: 12px;
    color: #1f2328;
    margin-top: 4px;
    font-weight: 500;
  }
  .hint {
    font-size: 11px;
    color: #6b7280;
    margin-top: 2px;
    line-height: 1.4;
  }
  .bars {
    display: flex;
    flex-direction: column;
    gap: 6px;
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
  .bar-fill.alt {
    background: #1f2328;
  }
  .bar-value {
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
  .ev-kind {
    color: #b45309;
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
  .degraded {
    background: #fef3c7;
    border: 1px solid #fde047;
    color: #92400e;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 16px;
    font-size: 13px;
  }
</style>
