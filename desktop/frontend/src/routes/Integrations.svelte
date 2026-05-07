<script lang="ts">
  import { onMount } from "svelte";
  import {
    ListUpstreams,
    UpstreamCatalog,
    GetHooksStatus,
  } from "../../wailsjs/go/main/App.js";

  let upstreams: any[] = [];
  let catalog: any[] = [];
  let hooks: any = null;
  let loading = true;
  let error: string | null = null;

  async function load() {
    loading = true;
    error = null;
    try {
      const [u, c, h] = await Promise.all([
        ListUpstreams().catch(() => ({ upstreams: [] })),
        UpstreamCatalog().catch(() => ({ entries: [] })),
        GetHooksStatus().catch(() => ({ installed: false })),
      ]);
      upstreams = u?.upstreams ?? [];
      catalog = c?.entries ?? [];
      hooks = h;
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  onMount(() => load());

  $: installedNames = new Set(upstreams.map((u: any) => u.name));
</script>

<header>
  <div>
    <h1>Integrations</h1>
    <div class="subtitle">
      Where memex connects to. MCP upstreams below; Claude Code hooks at the
      bottom; auto-detected AI tools (Cursor, Windsurf, Cline) on the right.
    </div>
  </div>
  <button on:click={() => load()}>refresh</button>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else}

  <h2>Claude Code hooks</h2>
  {#if hooks?.installed}
    <div class="hook-card on">
      <div class="hook-status">✓ memex hooks are wired into Claude Code</div>
      <div class="hook-events">
        Active for events:
        {#each Object.keys(hooks.events_wired ?? {}) as ev, i}
          <span class="ev-chip">{ev}</span>
        {/each}
      </div>
      <div class="muted small">
        settings: <code>{hooks.settings_path}</code>
      </div>
      <div class="muted small">
        To uninstall: edit <code>~/.claude/settings.json</code> and remove
        the memex command lines, or run <code>memex hooks-install</code>
        without <code>--apply</code> to re-print the canonical block.
      </div>
    </div>
  {:else}
    <div class="hook-card off">
      <div class="hook-status">○ Hooks not installed</div>
      <div class="muted small">
        Run <code>memex hooks-install --apply</code> to merge memex's
        UserPromptSubmit + PreToolUse + PostToolUse + Stop handlers into
        <code>~/.claude/settings.json</code>. Until then, the Dashboard
        Impact tiles stay at zero.
      </div>
    </div>
  {/if}

  <h2>Configured MCP upstreams ({upstreams.length})</h2>
  {#if upstreams.length === 0}
    <p class="muted">
      No upstreams configured. Pick from the catalog below or run
      <code>memex upstream install &lt;id&gt;</code>.
    </p>
  {:else}
    <div class="upstream-list">
      {#each upstreams as u}
        <div class="up-card">
          <div class="up-name">
            <span class="up-type">{u.type ?? "stdio"}</span>
            <strong>{u.name}</strong>
          </div>
          {#if u.command}
            <div class="up-target mono">{u.command} {(u.args ?? []).join(" ")}</div>
          {:else if u.url}
            <div class="up-target mono">{u.url}</div>
          {/if}
          <div class="up-meta">
            {#if u.auth?.kind && u.auth.kind !== "none"}
              <span class="up-auth">🔒 {u.auth.kind}</span>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <h2>Catalog ({catalog.length} available)</h2>
  <div class="muted small">
    Curated MCP servers memex can re-export through its gateway. Install via
    <code>memex upstream install &lt;id&gt;</code>; the daemon adds them to
    its config and the tools become callable from any AI tool talking to
    memex.
  </div>
  <div class="catalog-grid">
    {#each catalog as e}
      {@const installed = installedNames.has(e.name)}
      <div class="cat-card" class:installed>
        <div class="cat-head">
          <strong>{e.name}</strong>
          {#if installed}<span class="cat-chip on">installed</span>{/if}
          {#if e.type === "http" || e.target?.startsWith?.("http")}
            <span class="cat-chip http">http</span>
          {/if}
        </div>
        <div class="cat-desc">{e.description ?? e.summary ?? ""}</div>
        <div class="cat-meta">
          {#if e.category}<span>{e.category}</span>{/if}
          {#if e.tools?.length}<span>· {e.tools.length} tools</span>{/if}
        </div>
      </div>
    {/each}
  </div>
{/if}

<style>
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 24px;
    margin-bottom: 18px;
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
    max-width: 600px;
    line-height: 1.4;
  }
  h2 {
    margin: 22px 0 10px;
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
  .hook-card {
    border-radius: 8px;
    padding: 14px 18px;
  }
  .hook-card.on {
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
  }
  .hook-card.off {
    background: #fef9c3;
    border: 1px solid #fde047;
  }
  .hook-status {
    font-weight: 700;
    font-size: 14px;
    margin-bottom: 6px;
    color: #1f2328;
  }
  .hook-card.on .hook-status {
    color: #166534;
  }
  .hook-card.off .hook-status {
    color: #b45309;
  }
  .hook-events {
    font-size: 12px;
    color: #4b5563;
    margin-bottom: 6px;
  }
  .ev-chip {
    display: inline-block;
    background: #ffffff;
    border: 1px solid #bbf7d0;
    color: #166534;
    font-size: 11px;
    padding: 1px 8px;
    border-radius: 9px;
    margin-right: 4px;
    font-family: "Fira Code", monospace;
  }
  .upstream-list {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
    gap: 8px;
  }
  .up-card {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 10px 12px;
  }
  .up-name {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
  }
  .up-type {
    font-size: 10px;
    text-transform: uppercase;
    background: #f3f4f6;
    color: #57606a;
    padding: 1px 6px;
    border-radius: 9px;
    font-weight: 600;
  }
  .up-target {
    font-size: 11px;
    color: #57606a;
    word-break: break-all;
  }
  .up-meta {
    margin-top: 6px;
    font-size: 11px;
  }
  .up-auth {
    background: #fef3c7;
    color: #b45309;
    padding: 1px 7px;
    border-radius: 9px;
  }
  .catalog-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 8px;
  }
  .cat-card {
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 12px 14px;
  }
  .cat-card.installed {
    background: #f0fdf4;
    border-color: #bbf7d0;
  }
  .cat-head {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 4px;
  }
  .cat-chip {
    font-size: 9px;
    padding: 1px 6px;
    border-radius: 9px;
    text-transform: uppercase;
    font-weight: 700;
    letter-spacing: 0.4px;
  }
  .cat-chip.on {
    background: #bbf7d0;
    color: #166534;
  }
  .cat-chip.http {
    background: #dbeafe;
    color: #1e40af;
  }
  .cat-desc {
    font-size: 12px;
    color: #4b5563;
    line-height: 1.4;
  }
  .cat-meta {
    font-size: 11px;
    color: #6b7280;
    margin-top: 6px;
  }
  .mono {
    font-family: "Fira Code", monospace;
  }
  .small {
    font-size: 11px;
    line-height: 1.5;
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
