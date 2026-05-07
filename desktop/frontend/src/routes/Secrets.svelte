<script lang="ts">
  import { onMount } from "svelte";
  import {
    ListSecretsViaDaemon,
    PutSecret,
    DeleteSecret,
    RedactPreview,
  } from "../../wailsjs/go/main/App.js";

  let secrets: any[] = [];
  let loading = true;
  let error: string | null = null;

  // Add-secret modal state
  let showAdd = false;
  let newProvider = "";
  let newName = "";
  let newValue = "";
  let adding = false;

  // Redact-preview state
  let probeText = "";
  let probeResult: any = null;

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await ListSecretsViaDaemon();
      secrets = (res?.secrets ?? []) as any[];
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  async function addSecret() {
    if (!newProvider || !newName || !newValue) return;
    adding = true;
    try {
      await PutSecret(newProvider, newName, newValue);
      newProvider = ""; newName = ""; newValue = "";
      showAdd = false;
      await load();
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      adding = false;
    }
  }

  async function delSecret(provider: string, name: string) {
    if (!confirm(`Remove secret://${provider}/${name}?`)) return;
    try {
      await DeleteSecret(provider, name);
      await load();
    } catch (e: any) {
      error = String(e?.message || e);
    }
  }

  async function runProbe() {
    if (!probeText.trim()) {
      probeResult = null; return;
    }
    try {
      probeResult = await RedactPreview(probeText);
    } catch (e: any) {
      error = String(e?.message || e);
    }
  }

  function fmt(d: string | null): string {
    if (!d) return "—";
    return new Date(d).toLocaleString();
  }

  function copyHandle(handle: string) {
    navigator.clipboard?.writeText(handle).catch(() => {});
  }

  onMount(() => load());
</script>

<header>
  <div>
    <h1>Secrets</h1>
    <div class="subtitle">
      OS-keychain vault. memex stores values in Windows Credential Manager;
      the AI layer only ever sees <code>secret://provider/name</code> handles.
    </div>
  </div>
  <div class="controls">
    <button class="primary" on:click={() => (showAdd = true)}>+ Add secret</button>
    <button on:click={() => load()}>refresh</button>
  </div>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else}
  <h2>Vault ({secrets.length})</h2>
  {#if secrets.length === 0}
    <div class="empty">
      <p>No secrets stored yet.</p>
      <p class="muted">
        Click <em>Add secret</em> above, or run from the CLI:
        <code>memex secret put GITHUB ci-token --value &lt;value&gt;</code>
      </p>
    </div>
  {:else}
    <table class="vault">
      <thead>
        <tr>
          <th>handle</th>
          <th>created</th>
          <th>last resolved</th>
          <th>by</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each secrets as s (s.handle)}
          <tr>
            <td class="mono">
              <button class="link" on:click={() => copyHandle(s.handle)}
                title="click to copy">
                {s.handle}
              </button>
            </td>
            <td>{fmt(s.created_at)}</td>
            <td>{fmt(s.last_resolved_at)}</td>
            <td class="muted">{s.last_resolved_by ?? "—"}</td>
            <td>
              <button class="del" on:click={() => delSecret(s.provider, s.name)}>×</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <h2>Redact preview</h2>
  <div class="muted small">
    Paste a snippet (log line, transcript, env config) — see what memex would
    auto-redact before persisting it. <strong>Nothing</strong> is stored.
  </div>
  <div class="probe">
    <textarea
      placeholder="paste text containing API keys / tokens / JWTs…"
      bind:value={probeText}
      on:input={runProbe}
      rows="5"
    ></textarea>
    {#if probeResult}
      <div class="probe-result">
        <div class="probe-stats">
          {probeResult.events?.length ?? 0} match{probeResult.events?.length === 1 ? "" : "es"}:
          {#each probeResult.events ?? [] as ev}
            <span class="probe-chip">{ev.pattern_name}</span>
          {/each}
        </div>
        <pre class="redacted-text">{probeResult.redacted_text}</pre>
      </div>
    {/if}
  </div>
{/if}

{#if showAdd}
  <div class="modal-bg" on:click={() => (showAdd = false)} role="dialog">
    <div class="modal" on:click|stopPropagation>
      <h3>Add secret</h3>
      <p class="muted small">
        The value goes into the OS keychain immediately and is never
        echoed back. Once stored, only the handle is visible.
      </p>
      <label>
        Provider
        <input type="text" placeholder="github / openai / aws / tenant.acme"
               bind:value={newProvider} disabled={adding} />
      </label>
      <label>
        Name
        <input type="text" placeholder="ci-token / default-key / production-readonly"
               bind:value={newName} disabled={adding} />
      </label>
      <label>
        Value
        <input type="password" placeholder="<secret value>"
               bind:value={newValue} disabled={adding} />
      </label>
      <div class="modal-actions">
        <button on:click={() => (showAdd = false)} disabled={adding}>cancel</button>
        <button class="primary" on:click={addSecret}
                disabled={adding || !newProvider || !newName || !newValue}>
          {adding ? "storing…" : "store"}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    margin-bottom: 18px;
    gap: 24px;
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
    max-width: 540px;
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
  .controls {
    display: flex;
    gap: 8px;
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
  button.primary {
    background: #fef3c7;
    border-color: #fde047;
    font-weight: 600;
  }
  button.primary:hover:not(:disabled) {
    background: #fde047;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .empty {
    background: #fafbfc;
    border: 1px dashed #e6e8eb;
    border-radius: 8px;
    padding: 24px;
    text-align: center;
  }
  .empty code {
    background: #ffffff;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
    font-family: "Fira Code", monospace;
    border: 1px solid #e6e8eb;
  }
  table.vault {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  table.vault th {
    text-align: left;
    color: #6b7280;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    padding: 6px 10px;
    border-bottom: 1px solid #e6e8eb;
  }
  table.vault td {
    padding: 6px 10px;
    border-bottom: 1px solid #f3f4f6;
    color: #1f2328;
  }
  .mono {
    font-family: "Fira Code", monospace;
  }
  .link {
    background: transparent;
    border: 0;
    color: #1f6feb;
    cursor: pointer;
    padding: 0;
    font-family: inherit;
    font-size: inherit;
  }
  .link:hover {
    text-decoration: underline;
  }
  .del {
    background: transparent;
    border: 0;
    color: #9ca3af;
    font-size: 16px;
    line-height: 1;
    padding: 0 6px;
    cursor: pointer;
  }
  .del:hover {
    color: #dc2626;
    background: transparent;
  }
  .small {
    font-size: 11px;
    margin-bottom: 8px;
    line-height: 1.4;
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
  .probe textarea {
    width: 100%;
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 8px 10px;
    font-family: "Fira Code", monospace;
    font-size: 12px;
    resize: vertical;
  }
  .probe-result {
    margin-top: 10px;
  }
  .probe-stats {
    font-size: 11px;
    color: #57606a;
    margin-bottom: 6px;
  }
  .probe-chip {
    display: inline-block;
    background: #fee2e2;
    color: #991b1b;
    font-size: 10px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 9px;
    margin: 0 4px;
  }
  .redacted-text {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 8px 10px;
    font-family: "Fira Code", monospace;
    font-size: 11px;
    color: #1f2328;
    white-space: pre-wrap;
    margin: 0;
  }

  /* modal */
  .modal-bg {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.4);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 200;
  }
  .modal {
    background: #ffffff;
    border-radius: 10px;
    padding: 24px 28px;
    width: 420px;
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.25);
  }
  .modal h3 {
    margin: 0 0 8px;
    font-family: "Plus Jakarta Sans", sans-serif;
    font-weight: 700;
    font-size: 16px;
  }
  .modal label {
    display: block;
    margin-top: 12px;
    font-size: 11px;
    text-transform: uppercase;
    color: #6b7280;
    font-weight: 600;
    letter-spacing: 0.4px;
  }
  .modal input {
    display: block;
    width: 100%;
    margin-top: 4px;
    padding: 7px 10px;
    border: 1px solid #d0d7de;
    border-radius: 5px;
    font-family: inherit;
    font-size: 13px;
  }
  .modal-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 18px;
  }
</style>
