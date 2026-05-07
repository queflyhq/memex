<script lang="ts">
  import { onMount } from "svelte";
  import {
    UpdateTaskStatus,
    DeleteNode,
    GetNodeNeighborhood,
  } from "../../wailsjs/go/main/App.js";

  export let GetTasks: (status: string, limit: number) => Promise<any>;

  let tasks: any[] = [];
  let loading = true;
  let error: string | null = null;
  let dragId: string | null = null;
  let selected: any = null;
  let detail: any = null;
  let detailLoading = false;

  type Bucket = { key: string; label: string; rows: any[] };
  const COLUMN_KEYS = ["pending", "in_progress", "blocked", "completed", "cancelled"];
  const COLUMN_LABEL: Record<string, string> = {
    pending: "Pending",
    in_progress: "In Progress",
    blocked: "Blocked",
    completed: "Completed",
    cancelled: "Cancelled",
  };

  let columns: Bucket[] = [];
  let projects: any[] = [];

  function bucket(rows: any[]): Bucket[] {
    return COLUMN_KEYS.map((k) => ({
      key: k,
      label: COLUMN_LABEL[k],
      rows: rows.filter((t) => (t.metadata?.status ?? "pending") === k),
    }));
  }

  let projectIndex: Record<string, any> = {};
  let blockedByCounts: Record<string, number> = {};

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await GetTasks("*", 500);
      tasks = Array.isArray(res) ? res : (res?.tasks ?? []);
      projects = tasks.filter((t) => t.kind === "project");
      // Build a project lookup so cards can show their parent project
      // name + a blocker count without N edge fetches.
      projectIndex = Object.fromEntries(projects.map((p) => [p.id, p]));
      const taskRows = tasks.filter((t) => t.kind === "task");
      blockedByCounts = {};
      for (const t of taskRows) {
        const blockers = t.metadata?.blocked_by;
        if (Array.isArray(blockers) && blockers.length) {
          blockedByCounts[t.id] = blockers.length;
        }
      }
      columns = bucket(taskRows);
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  function projectFor(t: any): string | null {
    const pid = t.metadata?.project_id;
    if (!pid) return null;
    return projectIndex[pid]?.name ?? null;
  }

  async function moveTask(id: string, status: string) {
    const taskRows = tasks.filter((t) => t.kind === "task");
    const t = taskRows.find((x) => x.id === id);
    if (!t || (t.metadata?.status ?? "pending") === status) return;
    // Optimistic update.
    t.metadata = { ...(t.metadata || {}), status };
    columns = bucket(taskRows);
    try {
      await UpdateTaskStatus(id, status);
    } catch (e: any) {
      // Roll back.
      error = String(e?.message || e);
      await load();
    }
  }

  async function dropTask(taskID: string) {
    if (!confirm("Delete this task permanently? Its edges + history go too.")) return;
    try {
      await DeleteNode(taskID);
      tasks = tasks.filter((t) => t.id !== taskID);
      columns = bucket(tasks.filter((t) => t.kind === "task"));
      if (selected?.id === taskID) {
        selected = null;
        detail = null;
      }
    } catch (e: any) {
      error = String(e?.message || e);
    }
  }

  async function pickTask(t: any) {
    selected = t;
    detail = null;
    detailLoading = true;
    try {
      detail = await GetNodeNeighborhood(t.id);
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      detailLoading = false;
    }
  }

  function onDragStart(e: DragEvent, id: string) {
    dragId = id;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", id);
    }
  }

  function onDragOver(e: DragEvent) {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
  }

  function onDrop(e: DragEvent, status: string) {
    e.preventDefault();
    const id = dragId ?? e.dataTransfer?.getData("text/plain");
    dragId = null;
    if (id) moveTask(id, status);
  }

  function priorityColor(p: string): string {
    switch (p) {
      case "p0": return "#dc2626";
      case "p1": return "#ea580c";
      case "p2": return "#0891b2";
      case "p3": return "#737373";
      default:   return "#737373";
    }
  }

  function firstLine(s: string): string {
    if (!s) return "";
    const line = s.split("\n", 1)[0];
    return line.length > 140 ? line.slice(0, 138) + "…" : line;
  }

  function fmtDate(s: string): string {
    if (!s) return "";
    return new Date(s).toLocaleString();
  }

  onMount(() => load());
</script>

<header>
  <h1>Tasks &amp; Projects</h1>
  <div class="controls">
    <span class="muted">
      {#if !loading}{tasks.length} concepts ({projects.length} projects){/if}
    </span>
    <button on:click={() => load()}>refresh</button>
  </div>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else}
  {#if projects.length}
    <h2>Projects</h2>
    <div class="project-list">
      {#each projects as p}
        <button class="project-card" on:click={() => pickTask(p)}>
          <div class="proj-name">{p.name}</div>
          <div class="proj-desc">{firstLine(p.description ?? "")}</div>
          <div class="proj-meta">id: {p.id}</div>
        </button>
      {/each}
    </div>
  {/if}

  <h2>Tasks (drag a card to change status)</h2>
  <div class="kanban">
    {#each columns as col}
      <div
        class="col"
        on:dragover={onDragOver}
        on:drop={(e) => onDrop(e, col.key)}
      >
        <div class="col-head">
          <span>{col.label}</span>
          <span class="count">{col.rows.length}</span>
        </div>
        <div class="col-body">
          {#each col.rows as t}
            <div
              class="task"
              class:dragging={dragId === t.id}
              draggable="true"
              on:dragstart={(e) => onDragStart(e, t.id)}
              on:click={() => pickTask(t)}
              role="button"
              tabindex="0"
            >
              <div class="task-head">
                <span
                  class="prio"
                  style="background: {priorityColor(t.metadata?.priority)}"
                >
                  {t.metadata?.priority ?? "p?"}
                </span>
                <span class="task-name">{t.name}</span>
              </div>
              {#if t.description}
                <div class="task-desc">{firstLine(t.description)}</div>
              {/if}
              <div class="task-links">
                {#if projectFor(t)}
                  <span class="link link-project" title="parent project">
                    📁 {projectFor(t)}
                  </span>
                {/if}
                {#if blockedByCounts[t.id]}
                  <span class="link link-blocker" title="blocked by tasks">
                    🛇 {blockedByCounts[t.id]} blocker{blockedByCounts[t.id] > 1 ? "s" : ""}
                  </span>
                {/if}
                {#if t.metadata?.spawned_from}
                  <span class="link" title="event that spawned this">⚡ from event</span>
                {/if}
                {#if (t.last_confirmed_at && t.last_confirmed_at !== t.created_at)}
                  <span class="link" title="updated since creation">✎ edited</span>
                {/if}
              </div>
              <div class="task-meta">
                {#if t.metadata?.owner}<span>👤 {t.metadata.owner}</span>{/if}
                {#if t.metadata?.due}<span>⏰ {t.metadata.due}</span>{/if}
                <span class="open-hint">click for details →</span>
                <button
                  class="del-btn"
                  title="delete"
                  on:click|stopPropagation={() => dropTask(t.id)}
                >×</button>
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/each}
  </div>
{/if}

{#if selected}
  <aside class="detail">
    <button class="close" on:click={() => { selected = null; detail = null; }}>×</button>
    <div class="detail-head">
      <span class="kind">{selected.kind}</span>
      <h3>{selected.name}</h3>
      <div class="detail-meta">
        {#if selected.metadata?.status}<span class="chip">status: {selected.metadata.status}</span>{/if}
        {#if selected.metadata?.priority}<span class="chip">{selected.metadata.priority}</span>{/if}
        {#if selected.metadata?.owner}<span class="chip">👤 {selected.metadata.owner}</span>{/if}
        {#if selected.metadata?.due}<span class="chip">⏰ {selected.metadata.due}</span>{/if}
      </div>
    </div>
    {#if selected.description}
      <h4>Description</h4>
      <pre class="desc">{selected.description}</pre>
    {/if}
    <div class="ids">
      <span>id: <code>{selected.id}</code></span>
      <span>created: {fmtDate(selected.created_at)}</span>
    </div>

    {#if detailLoading}
      <p class="muted">loading associations…</p>
    {:else if detail}
      {#if detail.neighbors?.length}
        <h4>Linked concepts ({detail.neighbors.length})</h4>
        <div class="neighbor-list">
          {#each detail.neighbors as n}
            <div class="neighbor">
              <span class="kind sm">{n.kind}</span>
              <span class="n-name">{n.name}</span>
            </div>
          {/each}
        </div>
      {/if}
      {#if detail.edges?.length}
        <h4>Edges ({detail.edges.length})</h4>
        <div class="edges-list">
          {#each detail.edges as e}
            <div class="edge">
              <span class="dir">{e.from_id === selected.id ? "→" : "←"}</span>
              <span class="ek">{e.kind}</span>
              <span class="other">{e.from_id === selected.id ? e.to_id : e.from_id}</span>
            </div>
          {/each}
        </div>
      {/if}
      {#if detail.events?.length}
        <h4>Episodic events ({detail.events.length})</h4>
        <div class="events-list">
          {#each detail.events.slice(0, 30) as ev}
            <div class="event">
              <span class="time">{fmtDate(ev.timestamp).slice(11, 19)}</span>
              <span class="ekind">{ev.kind}</span>
              <span class="actor">{ev.actor}</span>
            </div>
          {/each}
        </div>
      {/if}
      {#if detail.history?.length}
        <h4>Version history ({detail.history.length})</h4>
        <div class="history-list">
          {#each detail.history as h}
            <div class="hist">
              <span class="hver">v{h.version}</span>
              <span class="time">{fmtDate(h.changed_at)}</span>
            </div>
          {/each}
        </div>
      {/if}
      {#if !detail.neighbors?.length && !detail.events?.length && !detail.history?.length}
        <p class="muted">no associations</p>
      {/if}
    {/if}
  </aside>
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
    font-family: "Plus Jakarta Sans", -apple-system, sans-serif;
    letter-spacing: -0.02em;
  }
  h2 {
    margin: 22px 0 10px;
    font-size: 12px;
    font-weight: 700;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.6px;
  }
  h4 {
    margin: 14px 0 6px;
    font-size: 11px;
    font-weight: 700;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 12px;
  }
  button {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    padding: 5px 12px;
    border-radius: 5px;
    font-size: 12px;
    font-family: inherit;
    cursor: pointer;
  }
  button:hover {
    background: #f3f4f6;
  }
  .project-list {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 10px;
    margin-bottom: 8px;
  }
  .project-card {
    background: #fef9c3;
    border: 1px solid #fde047;
    border-radius: 8px;
    padding: 12px 14px;
    text-align: left;
    cursor: pointer;
  }
  .project-card:hover {
    background: #fef3c7;
  }
  .proj-name {
    font-weight: 700;
    font-size: 13px;
    color: #1f2328;
  }
  .proj-desc {
    font-size: 12px;
    color: #4b5563;
    margin-top: 4px;
    line-height: 1.4;
  }
  .proj-meta {
    font-size: 11px;
    color: #6b7280;
    margin-top: 6px;
    font-family: "Fira Code", monospace;
  }
  .kanban {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 8px;
  }
  .col {
    background: #f7f8fa;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 8px;
    min-height: 240px;
  }
  .col-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 6px 8px;
    font-size: 11px;
    font-weight: 700;
    color: #1f2328;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .count {
    background: #e6e8eb;
    color: #57606a;
    padding: 1px 6px;
    border-radius: 9px;
    font-size: 10px;
    font-weight: 600;
  }
  .col-body {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .task {
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 8px 10px;
    box-shadow: 0 1px 0 rgba(31, 35, 40, 0.04);
    cursor: grab;
    transition: transform 0.05s ease;
  }
  .task:hover {
    border-color: #fbbf24;
  }
  .task.dragging {
    opacity: 0.5;
  }
  .task-head {
    display: flex;
    align-items: flex-start;
    gap: 7px;
    margin-bottom: 4px;
  }
  .prio {
    color: #ffffff;
    font-size: 9px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 9px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    flex-shrink: 0;
  }
  .task-name {
    font-size: 12px;
    font-weight: 500;
    color: #1f2328;
    line-height: 1.35;
  }
  .task-desc {
    font-size: 11px;
    color: #57606a;
    line-height: 1.4;
  }
  .task-links {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 6px;
  }
  .link {
    background: #f3f4f6;
    color: #57606a;
    padding: 1px 6px;
    border-radius: 9px;
    font-size: 10px;
    font-weight: 500;
    line-height: 1.4;
    white-space: nowrap;
  }
  .link-project {
    background: #fef3c7;
    color: #b45309;
  }
  .link-blocker {
    background: #fee2e2;
    color: #991b1b;
  }
  .task-meta {
    display: flex;
    gap: 8px;
    margin-top: 6px;
    font-size: 10px;
    color: #6b7280;
    align-items: center;
  }
  .open-hint {
    margin-left: auto;
    color: #9ca3af;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    opacity: 0;
    transition: opacity 0.15s ease;
  }
  .task:hover .open-hint {
    opacity: 1;
  }
  .del-btn {
    margin-left: auto;
    background: transparent;
    border: 0;
    color: #9ca3af;
    font-size: 16px;
    line-height: 1;
    padding: 0 4px;
    cursor: pointer;
  }
  .del-btn:hover {
    color: #dc2626;
    background: transparent;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }

  /* ---- Detail sidepanel ---- */
  .detail {
    position: fixed;
    right: 24px;
    top: 24px;
    bottom: 24px;
    width: 380px;
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 10px;
    box-shadow: 0 20px 50px rgba(31, 35, 40, 0.10);
    padding: 18px 20px;
    overflow-y: auto;
    z-index: 100;
  }
  .close {
    position: absolute;
    top: 10px;
    right: 10px;
    background: transparent;
    border: 0;
    color: #9ca3af;
    font-size: 22px;
    line-height: 1;
    cursor: pointer;
    padding: 0 6px;
  }
  .close:hover {
    color: #1f2328;
  }
  .detail-head h3 {
    margin: 6px 0 10px;
    font-size: 16px;
    font-weight: 700;
    color: #1f2328;
    font-family: "Plus Jakarta Sans", -apple-system, sans-serif;
    letter-spacing: -0.01em;
    line-height: 1.3;
  }
  .detail-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-bottom: 6px;
  }
  .chip {
    background: #f3f4f6;
    color: #1f2328;
    padding: 2px 8px;
    border-radius: 9px;
    font-size: 10px;
    font-weight: 600;
  }
  .kind {
    background: #fef3c7;
    color: #b45309;
    padding: 2px 8px;
    border-radius: 9px;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-weight: 700;
  }
  .kind.sm {
    font-size: 9px;
    padding: 1px 6px;
  }
  .desc {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 10px 12px;
    font-family: "Inter", -apple-system, sans-serif;
    font-size: 12px;
    line-height: 1.5;
    color: #1f2328;
    white-space: pre-wrap;
    word-wrap: break-word;
    margin: 6px 0;
    max-height: 280px;
    overflow-y: auto;
  }
  .ids {
    font-size: 11px;
    color: #6b7280;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .ids code {
    font-family: "Fira Code", monospace;
    color: #1f2328;
  }
  .neighbor-list, .edges-list, .events-list, .history-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
  }
  .neighbor {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 4px 6px;
    background: #fafbfc;
    border-radius: 4px;
  }
  .n-name {
    color: #1f2328;
    font-weight: 500;
  }
  .edge, .event, .hist {
    display: grid;
    grid-template-columns: 14px 1fr 1fr;
    gap: 6px;
    align-items: center;
    padding: 3px 6px;
    border-bottom: 1px solid #f3f4f6;
    font-size: 11px;
  }
  .edge .dir {
    color: #6b7280;
    text-align: center;
  }
  .edge .ek, .event .ekind {
    color: #b45309;
    font-weight: 500;
  }
  .edge .other, .event .actor {
    color: #1f2328;
    font-family: "Fira Code", monospace;
    font-size: 10px;
  }
  .hist .hver {
    color: #16a34a;
    font-weight: 700;
  }
  .event .time, .hist .time {
    color: #6b7280;
    font-family: "Fira Code", monospace;
    font-size: 10px;
  }
</style>
