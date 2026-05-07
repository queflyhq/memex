<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import {
    GetNodeNeighborhood,
    Recall,
    CreateConcept,
    PatchNode,
    DeleteNode,
  } from "../../wailsjs/go/main/App.js";

  export let GetConcepts: (kind: string, limit: number, offset: number) => Promise<any>;

  // Create / edit modal state
  let showCreate = false;
  let showEdit = false;
  let formName = "";
  let formKind = "fact";
  let formDesc = "";
  let formError = "";
  let formBusy = false;

  function openCreate() {
    formName = ""; formKind = "fact"; formDesc = ""; formError = "";
    showCreate = true; showEdit = false;
  }
  function openEdit() {
    if (!selected) return;
    formName = selected.name;
    formKind = selected.kind;
    formDesc = selected.description ?? "";
    formError = "";
    showEdit = true; showCreate = false;
  }
  async function submitCreate() {
    if (!formName.trim()) { formError = "name is required"; return; }
    formBusy = true; formError = "";
    try {
      const res = await CreateConcept(formName.trim(), formDesc, formKind);
      if (res?.error) {
        formError = res.error; return;
      }
      showCreate = false;
      await loadByKind();
    } catch (e: any) {
      formError = String(e?.message || e);
    } finally {
      formBusy = false;
    }
  }
  async function submitEdit() {
    if (!selected || !formName.trim()) { formError = "name is required"; return; }
    formBusy = true; formError = "";
    try {
      const res = await PatchNode(selected.id, {
        name: formName.trim(),
        description: formDesc,
        kind: formKind,
      });
      if (res?.error) { formError = res.error; return; }
      showEdit = false;
      // Refresh selected and the list.
      selected = res;
      await loadByKind();
    } catch (e: any) {
      formError = String(e?.message || e);
    } finally {
      formBusy = false;
    }
  }
  async function deleteCurrent() {
    if (!selected) return;
    if (!confirm(`Delete concept "${selected.name}"? This removes its edges + history too.`)) return;
    try {
      await DeleteNode(selected.id);
      selected = null; detail = null;
      await loadByKind();
    } catch (e: any) {
      error = String(e?.message || e);
    }
  }

  let kind = "";
  let concepts: any[] = [];
  let total = 0;
  let loading = true;
  let error: string | null = null;
  let q = "";
  let qPending = "";
  let searching = false;
  let searchHits: any[] | null = null;
  let detail: any = null;
  let detailLoading = false;
  let selected: any = null;

  const kindOptions = [
    "", "decision", "constraint", "fact", "pattern", "approach",
    "task", "project", "milestone", "module", "endpoint",
    "source", "file", "symbol", "person", "opinion", "question", "rejected",
  ];

  // Recall queries with debounce so typing doesn't hit the daemon every keystroke.
  let qTimer: number | undefined;
  function onQueryInput(e: Event) {
    qPending = (e.target as HTMLInputElement).value;
    if (qTimer) clearTimeout(qTimer);
    qTimer = window.setTimeout(runSearch, 250);
  }
  async function runSearch() {
    q = qPending;
    if (!q.trim()) {
      searchHits = null;
      return;
    }
    searching = true;
    try {
      const res = await Recall(q.trim(), 1500, 1);
      searchHits = (res?.nodes ?? []) as any[];
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      searching = false;
    }
  }

  async function loadByKind() {
    if (q.trim()) return; // search mode wins
    loading = true;
    error = null;
    try {
      const res = await GetConcepts(kind, 200, 0);
      concepts = res?.concepts ?? [];
      total = res?.total ?? concepts.length;
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  async function pickConcept(c: any) {
    selected = c;
    detail = null;
    detailLoading = true;
    try {
      detail = await GetNodeNeighborhood(c.id);
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      detailLoading = false;
    }
  }

  function firstLine(s: string): string {
    if (!s) return "";
    const line = s.split("\n", 1)[0];
    return line.length > 200 ? line.slice(0, 198) + "…" : line;
  }
  function fmt(d: string): string {
    if (!d) return "—";
    return new Date(d).toLocaleString();
  }

  onMount(() => loadByKind());
  $: if (kind !== undefined) loadByKind();

  // Visible list = search hits when in search mode, else by-kind.
  $: visibleList = searchHits ?? concepts;
  $: grouped = (() => {
    const groups: Record<string, any[]> = {};
    for (const c of visibleList) {
      (groups[c.kind] ??= []).push(c);
    }
    return Object.entries(groups).sort((a, b) => b[1].length - a[1].length);
  })();

  onDestroy(() => {
    if (qTimer) clearTimeout(qTimer);
  });

  function kindColor(k: string): string {
    const palette: Record<string, string> = {
      decision: "#fbbf24", constraint: "#f97316", fact: "#64748b",
      pattern: "#a855f7", approach: "#16a34a", task: "#22c55e",
      project: "#7c3aed", milestone: "#15803d", module: "#8b5cf6",
      endpoint: "#0ea5e9", source: "#dc2626", file: "#3b82f6",
      symbol: "#1f2328", person: "#06b6d4", opinion: "#94a3b8",
      question: "#ec4899", rejected: "#cbd5e1",
    };
    return palette[k] ?? "#6b7280";
  }
</script>

<header>
  <div>
    <h1>Concepts</h1>
    <div class="subtitle">
      Search across every captured decision / constraint / fact / code symbol /
      task. Filter by kind on the left. Click a card for the full neighborhood.
    </div>
  </div>
  <div class="actions">
    <button class="primary" on:click={openCreate}>+ New concept</button>
    <span class="muted small">
      {#if !loading && !searching}
        {visibleList.length}{searchHits ? "" : ` of ${total}`}
      {/if}
    </span>
  </div>
</header>

<div class="search-bar">
  <input
    type="text"
    placeholder="Search concepts (recall) — e.g. 'cross-repo linker' or 'JWT validation'"
    on:input={onQueryInput}
  />
  {#if searching}<span class="muted small">searching…</span>{/if}
  {#if searchHits !== null}
    <button class="link-btn" on:click={() => { qPending = ""; q = ""; searchHits = null; loadByKind(); }}>clear</button>
  {/if}
</div>

<div class="layout">
  <aside class="facets">
    <div class="facet-label">filter by kind</div>
    {#each kindOptions as k}
      <button
        class="facet-btn"
        class:active={kind === k}
        on:click={() => (kind = k)}
        disabled={!!searchHits}
      >
        <span class="facet-dot" style="background: {k ? kindColor(k) : '#9ca3af'}"></span>
        {k || "all kinds"}
      </button>
    {/each}
  </aside>

  <section class="list-pane">
    {#if loading}
      <p class="muted">loading…</p>
    {:else if error}
      <p class="error">error: {error}</p>
    {:else if visibleList.length === 0}
      <p class="muted">
        {searchHits !== null
          ? `No matches for "${q}"`
          : `No concepts of kind '${kind || "any"}'`}
      </p>
    {:else}
      {#each grouped as [groupKind, items] (groupKind)}
        <div class="group-head">
          <span class="kind-chip" style="background: {kindColor(groupKind)}22; color: {kindColor(groupKind)}">
            {groupKind}
          </span>
          <span class="count muted">{items.length}</span>
        </div>
        <div class="cards">
          {#each items as c (c.id)}
            <button class="card" class:selected={selected?.id === c.id} on:click={() => pickConcept(c)}>
              <div class="row">
                <span class="name">{c.name}</span>
                <span class="src" title="source actor">{c.source}</span>
              </div>
              {#if c.description}
                <div class="desc">{firstLine(c.description)}</div>
              {/if}
            </button>
          {/each}
        </div>
      {/each}
    {/if}
  </section>

  {#if selected}
    <aside class="detail">
      <button class="close" on:click={() => { selected = null; detail = null; }}>×</button>
      <span
        class="kind-chip big"
        style="background: {kindColor(selected.kind)}22; color: {kindColor(selected.kind)}"
      >
        {selected.kind}
      </span>
      <h3>{selected.name}</h3>
      <div class="provenance">
        <span>by <strong>{selected.source}</strong></span>
        <span>· created {fmt(selected.created_at)}</span>
      </div>
      <div class="detail-actions">
        <button on:click={openEdit}>edit</button>
        <button class="danger" on:click={deleteCurrent}>delete</button>
      </div>
      {#if selected.description}
        <pre class="desc-full">{selected.description}</pre>
      {/if}
      <div class="ids">
        <span>id: <code>{selected.id}</code></span>
      </div>

      {#if detailLoading}
        <p class="muted small">loading associations…</p>
      {:else if detail}
        {#if detail.neighbors?.length}
          <h4>Linked concepts ({detail.neighbors.length})</h4>
          <div class="neighbors">
            {#each detail.neighbors as n (n.id)}
              <button
                class="neighbor"
                on:click={() => pickConcept(n)}
                title="click to focus"
              >
                <span
                  class="kind-chip sm"
                  style="background: {kindColor(n.kind)}22; color: {kindColor(n.kind)}"
                >
                  {n.kind}
                </span>
                <span class="n-name">{n.name}</span>
              </button>
            {/each}
          </div>
        {/if}
        {#if detail.edges?.length}
          <h4>Edges ({detail.edges.length})</h4>
          <div class="edges-list">
            {#each detail.edges as e (e.from_id + ":" + e.to_id + ":" + e.kind)}
              <div class="edge">
                <span class="dir">{e.from_id === selected.id ? "→" : "←"}</span>
                <span class="ek">{e.kind}</span>
              </div>
            {/each}
          </div>
        {/if}
        {#if detail.events?.length}
          <h4>Episodic events ({detail.events.length})</h4>
          <div class="events-list">
            {#each detail.events.slice(0, 30) as ev (ev.id)}
              <div class="event">
                <span class="time">{fmt(ev.timestamp)}</span>
                <span class="ekind">{ev.kind}</span>
              </div>
            {/each}
          </div>
        {/if}
        {#if detail.history?.length}
          <h4>Version history ({detail.history.length})</h4>
          <div class="history-list">
            {#each detail.history as h}
              <div class="hist">v{h.version} · {fmt(h.changed_at)}</div>
            {/each}
          </div>
        {/if}
      {/if}
    </aside>
  {/if}
</div>

<style>
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 24px;
    margin-bottom: 14px;
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
  .meta {
    font-size: 12px;
    color: #6b7280;
  }

  .search-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 16px;
  }
  .search-bar input {
    flex: 1;
    padding: 8px 12px;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    font-size: 13px;
    font-family: inherit;
  }
  .search-bar input:focus {
    outline: 2px solid #fde047;
    outline-offset: -1px;
    border-color: #fbbf24;
  }
  .link-btn {
    background: transparent;
    border: 0;
    color: #1f6feb;
    cursor: pointer;
    font-family: inherit;
    font-size: 12px;
  }

  .layout {
    display: grid;
    grid-template-columns: 200px 1fr auto;
    gap: 16px;
  }
  .facets {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .facet-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: #6b7280;
    font-weight: 700;
    margin-bottom: 6px;
  }
  .facet-btn {
    display: flex;
    align-items: center;
    gap: 8px;
    background: transparent;
    border: 0;
    color: #57606a;
    text-align: left;
    padding: 6px 10px;
    border-radius: 5px;
    font-size: 12px;
    font-family: inherit;
    cursor: pointer;
  }
  .facet-btn:hover {
    background: #eef0f2;
    color: #1f2328;
  }
  .facet-btn.active {
    background: #fef3c7;
    color: #1f2328;
    font-weight: 600;
  }
  .facet-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .facet-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }

  .list-pane {
    overflow-y: auto;
  }
  .group-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 18px 0 6px;
  }
  .group-head:first-child {
    margin-top: 0;
  }
  .kind-chip {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 9px;
  }
  .kind-chip.big {
    font-size: 11px;
    padding: 3px 10px;
  }
  .kind-chip.sm {
    font-size: 9px;
    padding: 1px 6px;
  }
  .count {
    font-size: 11px;
    font-weight: 500;
  }
  .cards {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .card {
    text-align: left;
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 9px 12px;
    cursor: pointer;
    font-family: inherit;
  }
  .card:hover {
    border-color: #fde047;
    background: #fefce8;
  }
  .card.selected {
    border-color: #fbbf24;
    background: #fef3c7;
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
  }
  .name {
    font-size: 13px;
    font-weight: 600;
    color: #1f2328;
  }
  .src {
    font-size: 10px;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .desc {
    font-size: 12px;
    color: #4b5563;
    margin-top: 3px;
    line-height: 1.4;
  }

  .detail {
    width: 380px;
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 10px;
    box-shadow: 0 4px 16px rgba(31, 35, 40, 0.08);
    padding: 18px 20px;
    overflow-y: auto;
    max-height: calc(100vh - 200px);
    position: sticky;
    top: 0;
  }
  .close {
    position: absolute;
    top: 8px;
    right: 10px;
    background: transparent;
    border: 0;
    color: #9ca3af;
    font-size: 20px;
    cursor: pointer;
    font-family: inherit;
  }
  .detail h3 {
    margin: 8px 0 6px;
    font-size: 16px;
    font-weight: 700;
    color: #1f2328;
    font-family: "Plus Jakarta Sans", sans-serif;
    line-height: 1.3;
  }
  .detail h4 {
    margin: 16px 0 6px;
    font-size: 11px;
    font-weight: 700;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .provenance {
    font-size: 11px;
    color: #6b7280;
    display: flex;
    gap: 8px;
    margin-bottom: 8px;
  }
  .desc-full {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 10px 12px;
    font-family: "Inter", -apple-system, sans-serif;
    font-size: 12px;
    color: #1f2328;
    white-space: pre-wrap;
    word-wrap: break-word;
    line-height: 1.5;
    margin: 6px 0;
    max-height: 280px;
    overflow-y: auto;
  }
  .ids {
    font-size: 11px;
    color: #6b7280;
  }
  .ids code {
    color: #1f2328;
    font-family: "Fira Code", monospace;
  }
  .neighbors {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .neighbor {
    display: flex;
    align-items: center;
    gap: 7px;
    background: #fafbfc;
    border: 0;
    border-radius: 4px;
    padding: 5px 8px;
    cursor: pointer;
    font-family: inherit;
    text-align: left;
    width: 100%;
  }
  .neighbor:hover {
    background: #f3f4f6;
  }
  .n-name {
    color: #1f2328;
    font-size: 12px;
    font-weight: 500;
  }
  .edges-list, .events-list, .history-list {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 11px;
  }
  .edge, .event, .hist {
    display: grid;
    grid-template-columns: 18px 1fr 1fr;
    gap: 6px;
    padding: 3px 6px;
    border-bottom: 1px solid #f3f4f6;
  }
  .dir { color: #6b7280; text-align: center; }
  .ek, .ekind { color: #b45309; font-weight: 500; }
  .time { color: #6b7280; font-family: "Fira Code", monospace; font-size: 10px; }

  .small {
    font-size: 11px;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }
  code {
    background: #f3f4f6;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
    font-family: "Fira Code", monospace;
  }
</style>
