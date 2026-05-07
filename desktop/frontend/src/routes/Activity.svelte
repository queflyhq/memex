<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { GetEvents } from "../../wailsjs/go/main/App.js";

  let events: any[] = [];
  let loading = true;
  let error: string | null = null;
  let kindFilter = "";
  let pollHandle: number | undefined;

  const kindOptions = [
    "", "user_prompt", "tool_call", "concept_added", "edge_added",
    "auto_approval", "auto_deny", "secret_redacted",
    "user_correction", "file_reindexed", "afk_enabled", "afk_disabled",
  ];

  async function load(silent = false) {
    if (!silent) loading = true;
    error = null;
    try {
      const res = await GetEvents(kindFilter, 200);
      events = res.events ?? [];
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    load();
    pollHandle = window.setInterval(() => load(true), 5_000);
  });
  onDestroy(() => {
    if (pollHandle) clearInterval(pollHandle);
  });
  $: if (kindFilter !== undefined) load();

  function fmtTime(ts: string): string {
    if (!ts) return "";
    const d = new Date(ts);
    return d.toLocaleString();
  }
</script>

<header>
  <h1>Activity</h1>
  <div class="controls">
    <select bind:value={kindFilter}>
      {#each kindOptions as k}
        <option value={k}>{k || "(all kinds)"}</option>
      {/each}
    </select>
    <span class="muted">
      {#if !loading}{events.length} events{/if}
    </span>
  </div>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else if events.length === 0}
  <p class="muted">no events</p>
{:else}
  <div class="list">
    {#each events as ev}
      <div class="row">
        <span class="time">{fmtTime(ev.timestamp)}</span>
        <span class="kind">{ev.kind}</span>
        <span class="actor">{ev.actor}</span>
        {#if ev.payload && Object.keys(ev.payload).length}
          <details class="payload">
            <summary>payload</summary>
            <pre>{JSON.stringify(ev.payload, null, 2)}</pre>
          </details>
        {/if}
      </div>
    {/each}
  </div>
{/if}

<style>
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
  }
  h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
    color: #1f2328;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 12px;
  }
  select {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    padding: 5px 10px;
    border-radius: 4px;
    font-size: 12px;
    font-family: inherit;
  }
  .list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-family: "Fira Code", "Consolas", monospace;
    font-size: 12px;
  }
  .row {
    display: grid;
    grid-template-columns: 180px 220px 110px 1fr;
    align-items: center;
    gap: 10px;
    padding: 5px 8px;
    border-bottom: 1px solid #f3f4f6;
  }
  .time {
    color: #6b7280;
  }
  .kind {
    color: #b45309;
  }
  .actor {
    color: #1f2328;
  }
  .payload summary {
    color: #6b7280;
    cursor: pointer;
  }
  .payload pre {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    padding: 8px;
    border-radius: 4px;
    overflow-x: auto;
    margin-top: 4px;
    color: #1f2328;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }
</style>
