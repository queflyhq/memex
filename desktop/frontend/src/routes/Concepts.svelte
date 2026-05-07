<script lang="ts">
  import { onMount } from "svelte";

  export let GetConcepts: (kind: string, limit: number, offset: number) => Promise<any>;

  let kind = "";
  let concepts: any[] = [];
  let total = 0;
  let loading = true;
  let error: string | null = null;

  const kindOptions = [
    "", "decision", "constraint", "fact", "pattern", "approach",
    "task", "project", "milestone", "module", "endpoint",
    "source", "file", "symbol", "person", "opinion", "question", "rejected",
  ];

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await GetConcepts(kind, 200, 0);
      concepts = res.concepts ?? [];
      total = res.total ?? concepts.length;
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  onMount(() => load());
  $: if (kind !== undefined) load();

  function firstLine(s: string): string {
    if (!s) return "";
    const line = s.split("\n", 1)[0];
    return line.length > 160 ? line.slice(0, 158) + "…" : line;
  }
</script>

<header>
  <h1>Concepts</h1>
  <div class="controls">
    <select bind:value={kind}>
      {#each kindOptions as k}
        <option value={k}>{k || "(all kinds)"}</option>
      {/each}
    </select>
    <span class="muted">
      {#if !loading}{concepts.length} of {total}{/if}
    </span>
  </div>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else if concepts.length === 0}
  <p class="muted">no concepts of kind '{kind || "any"}'</p>
{:else}
  <div class="list">
    {#each concepts as c}
      <div class="card">
        <div class="row">
          <span class="kind">{c.kind}</span>
          <span class="name">{c.name}</span>
        </div>
        {#if c.description}
          <div class="desc">{firstLine(c.description)}</div>
        {/if}
        <div class="meta">
          <span>id: {c.id}</span>
          <span>· src: {c.source}</span>
          {#if c.confidence != null}
            <span>· conf: {c.confidence.toFixed(2)}</span>
          {/if}
        </div>
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
    gap: 8px;
  }
  .card {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 10px 14px;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 4px;
  }
  .kind {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #b45309;
    font-weight: 600;
  }
  .name {
    font-size: 13px;
    font-weight: 600;
    color: #1f2328;
  }
  .desc {
    font-size: 12px;
    color: #4b5563;
    line-height: 1.4;
  }
  .meta {
    font-size: 11px;
    color: #6b7280;
    margin-top: 4px;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }
</style>
