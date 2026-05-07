<script lang="ts">
  import { onMount } from "svelte";
  import {
    AddCodeSource,
    ListCodeSources,
    GetConcepts,
    GetNodeNeighborhood,
  } from "../../wailsjs/go/main/App.js";

  let sources: any[] = [];
  let loading = true;
  let error: string | null = null;
  let newPath = "";
  let adding = false;
  let addOutput = "";

  let activeSource: any = null;
  let activeFiles: any[] = [];
  let filesLoading = false;

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await ListCodeSources();
      sources = (res?.concepts ?? []).sort((a: any, b: any) =>
        (a.created_at ?? "").localeCompare(b.created_at ?? "")
      );
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  async function addSource() {
    if (!newPath.trim()) return;
    adding = true;
    addOutput = "";
    error = null;
    try {
      const res = await AddCodeSource(newPath, true);
      addOutput = res?.output ?? "";
      if (res?.error) error = res.error;
      newPath = "";
      await load();
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      adding = false;
    }
  }

  async function pickSource(src: any) {
    activeSource = src;
    activeFiles = [];
    filesLoading = true;
    try {
      const res = await GetConcepts("file", 500, 0);
      const all = res?.concepts ?? [];
      activeFiles = all.filter(
        (f: any) => f.metadata?.source_id === src.id,
      );
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      filesLoading = false;
    }
  }

  function fmt(d: string): string {
    if (!d) return "—";
    return new Date(d).toLocaleString();
  }

  onMount(() => load());
</script>

<header>
  <h1>Sources</h1>
  <div class="controls">
    <input
      type="text"
      placeholder="C:\path\to\codebase"
      bind:value={newPath}
      disabled={adding}
    />
    <button on:click={addSource} disabled={adding || !newPath.trim()}>
      {adding ? "indexing…" : "add + index"}
    </button>
    <button on:click={() => load()}>refresh</button>
  </div>
</header>

{#if addOutput}
  <pre class="out">{addOutput}</pre>
{/if}

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else if sources.length === 0}
  <div class="empty">
    <p class="muted">No codebases registered yet.</p>
    <p class="muted">
      Try: <code>memex source add C:\Users\Admin\memex</code> from the CLI,
      or paste a path above and click <em>add + index</em>.
    </p>
  </div>
{:else}
  <div class="layout">
    <div class="src-list">
      {#each sources as s}
        <button
          class="src-card"
          class:active={activeSource?.id === s.id}
          on:click={() => pickSource(s)}
        >
          <div class="s-name">{s.name}</div>
          <div class="s-path">{s.metadata?.path ?? "—"}</div>
          <div class="s-stats">
            <span>{s.metadata?.indexed_files ?? 0} files</span>
            <span>·</span>
            <span>{s.metadata?.indexed_symbols ?? 0} symbols</span>
          </div>
          <div class="s-time">last indexed: {fmt(s.metadata?.last_indexed_at)}</div>
        </button>
      {/each}
    </div>

    <div class="src-detail">
      {#if !activeSource}
        <p class="muted">Select a source to see its files.</p>
      {:else if filesLoading}
        <p class="muted">loading files…</p>
      {:else}
        <h2>{activeSource.name}</h2>
        <div class="meta">
          <span>id: <code>{activeSource.id}</code></span>
          <span>path: <code>{activeSource.metadata?.path}</code></span>
        </div>
        <h3>Files ({activeFiles.length})</h3>
        {#if activeFiles.length === 0}
          <p class="muted">No files indexed yet for this source.</p>
        {:else}
          <div class="files">
            {#each activeFiles as f}
              <div class="file">
                <span class="lang">{f.metadata?.language ?? "?"}</span>
                <span class="path">{f.name}</span>
              </div>
            {/each}
          </div>
        {/if}
      {/if}
    </div>
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
    font-family: "Plus Jakarta Sans", -apple-system, sans-serif;
    letter-spacing: -0.02em;
  }
  h2 {
    margin: 4px 0 8px;
    font-size: 15px;
    font-weight: 700;
    color: #1f2328;
  }
  h3 {
    margin: 14px 0 8px;
    font-size: 11px;
    font-weight: 700;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
  }
  input[type="text"] {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    padding: 5px 10px;
    border-radius: 5px;
    font-size: 12px;
    font-family: inherit;
    width: 320px;
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
  button:hover:not(:disabled) {
    background: #f3f4f6;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .empty {
    padding: 24px;
    text-align: center;
    background: #fafbfc;
    border: 1px dashed #e6e8eb;
    border-radius: 8px;
  }
  .empty code {
    background: #f3f4f6;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
    font-family: "Fira Code", monospace;
  }
  .out {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 8px 12px;
    font-family: "Fira Code", monospace;
    font-size: 11px;
    color: #1f2328;
    white-space: pre-wrap;
    margin-bottom: 16px;
    max-height: 200px;
    overflow-y: auto;
  }
  .layout {
    display: grid;
    grid-template-columns: 360px 1fr;
    gap: 16px;
  }
  .src-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .src-card {
    text-align: left;
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 10px 14px;
    cursor: pointer;
  }
  .src-card:hover {
    background: #f3f4f6;
  }
  .src-card.active {
    background: #fef3c7;
    border-color: #fde047;
  }
  .s-name {
    font-weight: 700;
    font-size: 13px;
    color: #1f2328;
  }
  .s-path {
    font-size: 11px;
    color: #6b7280;
    font-family: "Fira Code", monospace;
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .s-stats {
    font-size: 11px;
    color: #57606a;
    margin-top: 6px;
    display: flex;
    gap: 6px;
  }
  .s-time {
    font-size: 10px;
    color: #6b7280;
    margin-top: 2px;
  }
  .src-detail {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 14px 18px;
    overflow-y: auto;
    max-height: calc(100vh - 200px);
  }
  .meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 11px;
    color: #6b7280;
  }
  .meta code {
    color: #1f2328;
    font-family: "Fira Code", monospace;
  }
  .files {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 11px;
    font-family: "Fira Code", monospace;
  }
  .file {
    display: grid;
    grid-template-columns: 80px 1fr;
    align-items: center;
    padding: 3px 6px;
    border-bottom: 1px solid #f3f4f6;
  }
  .lang {
    color: #b45309;
    font-size: 10px;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.4px;
  }
  .path {
    color: #1f2328;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }
</style>
