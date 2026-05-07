<script lang="ts">
  import { onMount } from "svelte";

  export let GetTasks: (status: string, limit: number) => Promise<any>;

  let tasks: any[] = [];
  let loading = true;
  let error: string | null = null;
  let statusFilter = "*";

  type Bucket = { key: string; label: string; rows: any[] };

  const COLUMNS: Bucket[] = [
    { key: "pending", label: "Pending", rows: [] },
    { key: "in_progress", label: "In Progress", rows: [] },
    { key: "blocked", label: "Blocked", rows: [] },
    { key: "completed", label: "Completed", rows: [] },
    { key: "cancelled", label: "Cancelled", rows: [] },
  ];

  let columns: Bucket[] = COLUMNS.map((c) => ({ ...c, rows: [] }));
  let projects: any[] = [];

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await GetTasks(statusFilter, 500);
      tasks = Array.isArray(res) ? res : (res?.tasks ?? []);
      projects = tasks.filter((t) => t.kind === "project");
      const taskRows = tasks.filter((t) => t.kind === "task");
      // Bucket by status.
      columns = COLUMNS.map((c) => ({
        ...c,
        rows: taskRows.filter(
          (t) => (t.metadata?.status ?? "pending") === c.key,
        ),
      }));
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  onMount(() => load());

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
        <div class="project-card">
          <div class="proj-name">{p.name}</div>
          <div class="proj-desc">{firstLine(p.description ?? "")}</div>
          <div class="proj-meta">id: {p.id}</div>
        </div>
      {/each}
    </div>
  {/if}

  <h2>Tasks (kanban)</h2>
  <div class="kanban">
    {#each columns as col}
      <div class="col">
        <div class="col-head">
          <span>{col.label}</span>
          <span class="count">{col.rows.length}</span>
        </div>
        <div class="col-body">
          {#each col.rows as t}
            <div class="task">
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
              <div class="task-meta">
                {#if t.metadata?.owner}
                  <span>👤 {t.metadata.owner}</span>
                {/if}
                {#if t.metadata?.due}
                  <span>⏰ {t.metadata.due}</span>
                {/if}
              </div>
            </div>
          {/each}
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
    margin-bottom: 18px;
  }
  h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
    color: #1f2328;
  }
  h2 {
    margin: 22px 0 10px;
    font-size: 12px;
    font-weight: 600;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.6px;
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
  }
  .proj-name {
    font-weight: 600;
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
  }
  .kanban {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 8px;
    margin-top: 4px;
  }
  .col {
    background: #f7f8fa;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 8px;
    min-height: 200px;
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
  .task-meta {
    display: flex;
    gap: 8px;
    margin-top: 6px;
    font-size: 10px;
    color: #6b7280;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }
</style>
