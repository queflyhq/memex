<script lang="ts">
  import { onMount } from "svelte";
  import {
    AddCodeSource,
    ListCodeSources,
    GetConcepts,
    FileSymbols,
  } from "../../wailsjs/go/main/App.js";

  // File-drilldown state
  let activeFile: any = null;
  let fileSymbols: any[] = [];
  let fileSymbolsLoading = false;
  let activeSymbol: any = null;

  async function pickFile(f: any) {
    activeFile = f;
    activeSymbol = null;
    fileSymbols = [];
    fileSymbolsLoading = true;
    try {
      const res = await FileSymbols(f.metadata.source_id, f.id);
      fileSymbols = res?.symbols ?? [];
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      fileSymbolsLoading = false;
    }
  }

  function pickSymbol(s: any) {
    activeSymbol = s;
  }

  // Language icons sourced from authfi-website. Missing icons fall back
  // to a colored letter chip.
  import pythonIco from "../assets/langs/python.svg";
  import goIco from "../assets/langs/go.svg";
  import javaIco from "../assets/langs/java.svg";
  import nodejsIco from "../assets/langs/nodejs.svg";
  import terraformIco from "../assets/langs/terraform.svg";

  const LANG_ICON: Record<string, string> = {
    python: pythonIco,
    go: goIco,
    java: javaIco,
    javascript: nodejsIco,
    typescript: nodejsIco,
    hcl: terraformIco,
  };
  const LANG_FALLBACK_COLOR: Record<string, string> = {
    bash:    "#4eaa25",
    yaml:    "#cb171e",
    svelte:  "#ff3e00",
    rust:    "#dea584",
    cpp:     "#00599c",
    csharp:  "#239120",
    ruby:    "#cc342d",
  };

  let sources: any[] = [];
  let loading = true;
  let error: string | null = null;
  let newPath = "";
  let adding = false;
  let addOutput = "";

  let activeSource: any = null;
  let activeFiles: any[] = [];
  let filesLoading = false;

  function langCount(src: any): [string, number][] {
    const langs = src.metadata?.languages_breakdown ?? {};
    return Object.entries(langs as Record<string, number>).sort(
      (a, b) => b[1] - a[1],
    );
  }

  // Group active files by language.
  $: filesByLang = (() => {
    const out: Record<string, any[]> = {};
    for (const f of activeFiles) {
      const l = f.metadata?.language ?? "?";
      (out[l] ??= []).push(f);
    }
    return Object.entries(out).sort((a, b) => b[1].length - a[1].length);
  })();

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

        {#if filesByLang.length > 0}
          <h3>Languages</h3>
          <div class="lang-row">
            {#each filesByLang as [l, files]}
              <div class="lang-chip">
                {#if LANG_ICON[l]}
                  <img src={LANG_ICON[l]} alt={l} />
                {:else}
                  <span
                    class="lang-letter"
                    style="background: {LANG_FALLBACK_COLOR[l] ?? '#6b7280'}"
                  >{l[0]?.toUpperCase()}</span>
                {/if}
                <span class="lang-name">{l}</span>
                <span class="lang-count">{files.length}</span>
              </div>
            {/each}
          </div>
        {/if}

        <h3>Files ({activeFiles.length})</h3>
        {#if activeFiles.length === 0}
          <p class="muted">No files indexed yet for this source.</p>
        {:else}
          <div class="three-pane">
            <div class="files">
              {#each activeFiles as f (f.id)}
                <button
                  class="file"
                  class:active={activeFile?.id === f.id}
                  on:click={() => pickFile(f)}
                >
                  {#if LANG_ICON[f.metadata?.language]}
                    <img class="row-ico" src={LANG_ICON[f.metadata.language]} alt={f.metadata.language} />
                  {:else}
                    <span
                      class="row-letter"
                      style="background: {LANG_FALLBACK_COLOR[f.metadata?.language] ?? '#6b7280'}"
                    >{(f.metadata?.language ?? "?")[0]?.toUpperCase()}</span>
                  {/if}
                  <span class="path">{f.name}</span>
                </button>
              {/each}
            </div>

            <div class="symbols-pane">
              {#if !activeFile}
                <p class="muted small">click a file to see its symbols</p>
              {:else if fileSymbolsLoading}
                <p class="muted small">loading symbols…</p>
              {:else if fileSymbols.length === 0}
                <p class="muted small">No symbols indexed for this file (config / docs / non-typed lang).</p>
              {:else}
                <div class="symbols-head">
                  <span class="muted">{fileSymbols.length} symbol{fileSymbols.length === 1 ? "" : "s"} in</span>
                  <span class="mono">{activeFile.name}</span>
                </div>
                <div class="symbols">
                  {#each fileSymbols as s (s.id)}
                    <button
                      class="symbol"
                      class:active={activeSymbol?.id === s.id}
                      on:click={() => pickSymbol(s)}
                    >
                      <span class="sk-chip" title="{s.metadata?.symbol_kind}">
                        {s.metadata?.symbol_kind?.[0]?.toUpperCase() ?? "?"}
                      </span>
                      <span class="s-name">{s.name}</span>
                      <span class="s-line">L{s.metadata?.start_line}</span>
                    </button>
                  {/each}
                </div>
              {/if}
            </div>

            <div class="symbol-detail">
              {#if !activeSymbol}
                <p class="muted small">click a symbol to see its signature + body</p>
              {:else}
                <div class="sd-head">
                  <span class="kind-chip">{activeSymbol.metadata?.symbol_kind}</span>
                  <strong>{activeSymbol.name}</strong>
                  <span class="muted small">L{activeSymbol.metadata?.start_line}-{activeSymbol.metadata?.end_line}</span>
                </div>
                {#if activeSymbol.metadata?.signature}
                  <pre class="sig">{activeSymbol.metadata.signature}</pre>
                {/if}
                {#if activeSymbol.metadata?.docstring}
                  <h4>Docstring</h4>
                  <pre class="doc">{activeSymbol.metadata.docstring}</pre>
                {/if}
                {#if activeSymbol.metadata?.annotations?.length}
                  <h4>Annotations</h4>
                  <div class="annot">
                    {#each activeSymbol.metadata.annotations as a}
                      <span class="ann-chip">@{a.name}{a.args}</span>
                    {/each}
                  </div>
                {/if}
                {#if activeSymbol.metadata?.body}
                  <h4>Body (preview)</h4>
                  <pre class="body">{activeSymbol.metadata.body}</pre>
                {/if}
                <div class="muted small">id: <code>{activeSymbol.id}</code></div>
              {/if}
            </div>
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
  .lang-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 12px;
  }
  .lang-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #ffffff;
    border: 1px solid #e6e8eb;
    padding: 4px 10px 4px 5px;
    border-radius: 14px;
    font-size: 11px;
  }
  .lang-chip img {
    width: 16px;
    height: 16px;
    object-fit: contain;
  }
  .lang-letter, .row-letter {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    border-radius: 4px;
    color: #ffffff;
    font-weight: 700;
    font-size: 10px;
    flex-shrink: 0;
  }
  .lang-name {
    color: #1f2328;
    font-weight: 500;
  }
  .lang-count {
    color: #6b7280;
    font-variant-numeric: tabular-nums;
  }
  .files {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 11px;
    font-family: "Fira Code", monospace;
  }
  .three-pane {
    display: grid;
    grid-template-columns: 1fr 1fr 1.4fr;
    gap: 12px;
    height: calc(100vh - 320px);
    min-height: 320px;
  }
  .files {
    display: flex;
    flex-direction: column;
    gap: 1px;
    overflow-y: auto;
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 4px;
  }
  .file {
    display: grid;
    grid-template-columns: 24px 1fr;
    align-items: center;
    gap: 8px;
    padding: 5px 8px;
    border: 0;
    background: transparent;
    border-radius: 4px;
    text-align: left;
    cursor: pointer;
    font-family: "Fira Code", monospace;
    font-size: 11px;
    color: #1f2328;
  }
  .file:hover {
    background: #eef0f2;
  }
  .file.active {
    background: #fef3c7;
  }
  .symbols-pane, .symbol-detail {
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 8px 10px;
    overflow-y: auto;
  }
  .symbols-head {
    display: flex;
    gap: 6px;
    align-items: baseline;
    padding: 2px 4px 8px;
    border-bottom: 1px solid #f3f4f6;
    margin-bottom: 6px;
    font-size: 11px;
  }
  .symbols {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .symbol {
    display: grid;
    grid-template-columns: 22px 1fr 36px;
    align-items: center;
    gap: 6px;
    padding: 4px 6px;
    border: 0;
    background: transparent;
    border-radius: 4px;
    text-align: left;
    cursor: pointer;
    font-family: "Fira Code", monospace;
    font-size: 11px;
    color: #1f2328;
  }
  .symbol:hover { background: #eef0f2; }
  .symbol.active { background: #fef3c7; }
  .sk-chip {
    background: #fbbf24;
    color: #1f2328;
    font-weight: 700;
    font-size: 10px;
    text-align: center;
    border-radius: 4px;
  }
  .s-line {
    color: #6b7280;
    font-size: 10px;
    text-align: right;
  }
  .sd-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .kind-chip {
    background: #fef3c7;
    color: #b45309;
    padding: 2px 8px;
    border-radius: 9px;
    font-size: 10px;
    text-transform: uppercase;
    font-weight: 700;
    letter-spacing: 0.4px;
  }
  .sig {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 4px;
    padding: 6px 8px;
    font-family: "Fira Code", monospace;
    font-size: 11px;
    margin: 0 0 8px;
    white-space: pre-wrap;
    color: #1f2328;
  }
  .doc {
    background: #fef9c3;
    border: 1px solid #fde047;
    border-radius: 4px;
    padding: 6px 8px;
    font-family: "Inter", sans-serif;
    font-size: 11px;
    margin: 0 0 8px;
    white-space: pre-wrap;
    line-height: 1.5;
    color: #1f2328;
  }
  .annot {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-bottom: 8px;
  }
  .ann-chip {
    background: #f3f4f6;
    color: #1f2328;
    padding: 1px 7px;
    border-radius: 9px;
    font-family: "Fira Code", monospace;
    font-size: 10px;
  }
  .body {
    background: #0f172a;
    color: #e2e8f0;
    border-radius: 4px;
    padding: 8px 10px;
    font-family: "Fira Code", monospace;
    font-size: 11px;
    margin: 0 0 8px;
    white-space: pre-wrap;
    overflow-x: auto;
    max-height: 280px;
    line-height: 1.45;
  }
  /* legacy single-column layout — kept for reference but overridden by .three-pane */
  .legacy-file-row {
    display: grid;
    grid-template-columns: 24px 1fr;
    align-items: center;
    gap: 8px;
    padding: 3px 6px;
    border-bottom: 1px solid #f3f4f6;
  }
  .row-ico {
    width: 16px;
    height: 16px;
    object-fit: contain;
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
