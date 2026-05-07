<script lang="ts">
  import { onMount } from "svelte";
  import { GetSchema, GetTableRows } from "../../wailsjs/go/main/App.js";

  let schema: any = null;
  let activeTable: string | null = null;
  let activeRows: any = null;
  let loading = true;
  let error: string | null = null;

  async function loadSchema() {
    loading = true;
    error = null;
    try {
      schema = await GetSchema();
      const tables = schema?.tables ?? [];
      if (tables.length && !activeTable) {
        activeTable = tables[0].name;
        await loadRows(activeTable);
      }
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  async function loadRows(table: string) {
    try {
      activeRows = await GetTableRows(table, 50, 0);
    } catch (e: any) {
      error = String(e?.message || e);
    }
  }

  function pickTable(name: string) {
    activeTable = name;
    activeRows = null;
    loadRows(name);
  }

  function tableDef(name: string | null): any {
    if (!schema || !name) return null;
    const tables = schema.tables ?? [];
    return tables.find((t: any) => t.name === name) ?? null;
  }

  function cellText(row: any, col: string): string {
    const v = row[col];
    if (v === null || v === undefined) return "—";
    if (typeof v === "object") return JSON.stringify(v).slice(0, 100);
    return String(v).slice(0, 100);
  }

  onMount(() => loadSchema());
</script>

<header>
  <h1>Database</h1>
  <button on:click={() => loadSchema()}>refresh</button>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else if !schema}
  <p class="muted">no schema</p>
{:else}
  <div class="db-layout">
    <aside class="tables">
      <div class="section-label">DuckDB tables</div>
      {#each schema.tables ?? [] as t}
        <button
          class="table-btn"
          class:active={activeTable === t.name}
          on:click={() => pickTable(t.name)}
        >
          <span class="t-name">{t.name}</span>
          <span class="t-count">{t.row_count ?? "—"}</span>
        </button>
      {/each}
    </aside>

    <section class="content">
      {#if activeTable}
        <div class="content-head">
          <h2>{activeTable}</h2>
          <span class="muted">{tableDef(activeTable)?.row_count ?? "?"} rows</span>
        </div>

        <h3>Schema</h3>
        <table class="schema-table">
          <thead>
            <tr>
              <th>column</th>
              <th>type</th>
              <th>nullable</th>
            </tr>
          </thead>
          <tbody>
            {#each tableDef(activeTable)?.columns ?? [] as col}
              <tr>
                <td class="mono">{col.name}</td>
                <td class="mono dim">{col.type}</td>
                <td class="dim">{col.nullable ? "yes" : "no"}</td>
              </tr>
            {/each}
          </tbody>
        </table>

        {#if activeRows}
          <h3>Sample rows ({activeRows.rows?.length ?? 0} of {tableDef(activeTable)?.row_count ?? "?"})</h3>
          <div class="rows-wrap">
            <table class="rows-table">
              <thead>
                <tr>
                  {#each activeRows.columns as c}
                    <th>{c}</th>
                  {/each}
                </tr>
              </thead>
              <tbody>
                {#each activeRows.rows as row}
                  <tr>
                    {#each activeRows.columns as c}
                      <td class="mono">{cellText(row, c)}</td>
                    {/each}
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      {/if}
    </section>
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
  h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: #1f2328;
  }
  h3 {
    margin: 18px 0 8px;
    font-size: 11px;
    font-weight: 700;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.6px;
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
  .db-layout {
    display: grid;
    grid-template-columns: 240px 1fr;
    gap: 16px;
    height: calc(100vh - 120px);
  }
  .tables {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 8px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    overflow-y: auto;
  }
  .section-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #6b7280;
    font-weight: 700;
    padding: 6px 8px 8px;
  }
  .table-btn {
    background: transparent;
    border: 0;
    color: #1f2328;
    text-align: left;
    padding: 7px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
    font-family: inherit;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .table-btn:hover {
    background: #eef0f2;
  }
  .table-btn.active {
    background: #fef3c7;
    font-weight: 600;
  }
  .t-name {
    font-family: "Fira Code", "Consolas", monospace;
    font-size: 12px;
  }
  .t-count {
    color: #6b7280;
    font-size: 11px;
    font-variant-numeric: tabular-nums;
  }
  .content {
    overflow-y: auto;
    padding-right: 8px;
  }
  .content-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    border-bottom: 1px solid #e6e8eb;
    padding-bottom: 8px;
  }
  .schema-table, .rows-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .schema-table th, .rows-table th {
    text-align: left;
    color: #6b7280;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    padding: 6px 10px;
    border-bottom: 1px solid #e6e8eb;
    background: #fafbfc;
  }
  .schema-table td, .rows-table td {
    padding: 5px 10px;
    border-bottom: 1px solid #f3f4f6;
    color: #1f2328;
    vertical-align: top;
  }
  .mono {
    font-family: "Fira Code", "Consolas", monospace;
  }
  .dim {
    color: #6b7280;
  }
  .rows-wrap {
    overflow-x: auto;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }
</style>
