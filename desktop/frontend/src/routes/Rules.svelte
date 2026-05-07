<script lang="ts">
  import { onMount } from "svelte";

  export let GetConcepts: (kind: string, limit: number, offset: number) => Promise<any>;

  let approvalPolicies: any[] = [];
  let hardDeny: any[] = [];
  let otherConstraints: any[] = [];
  let afkFlag: any = null;
  let loading = true;
  let error: string | null = null;

  async function load() {
    loading = true;
    error = null;
    try {
      const cs = await GetConcepts("constraint", 500, 0);
      const all = cs?.concepts ?? [];
      approvalPolicies = all.filter(
        (c: any) => c.metadata?.policy_type === "approval",
      );
      hardDeny = all.filter(
        (c: any) => c.metadata?.policy_type === "hard_deny",
      );
      otherConstraints = all.filter(
        (c: any) =>
          c.metadata?.policy_type !== "approval" &&
          c.metadata?.policy_type !== "hard_deny",
      );

      const facts = await GetConcepts("fact", 500, 0);
      const flag = (facts?.concepts ?? []).find(
        (c: any) => c.metadata?.signal === "afk_mode",
      );
      afkFlag = flag || null;
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  function fmtArgs(m: any): string {
    if (!m) return "";
    return JSON.stringify(m);
  }

  function fmt(d: string): string {
    if (!d) return "—";
    return new Date(d).toLocaleString();
  }

  onMount(() => load());
</script>

<header>
  <h1>Rules &amp; AFK</h1>
  <button on:click={() => load()}>refresh</button>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else}
  <h2>AFK mode</h2>
  {#if afkFlag}
    <div class="afk on">
      <div class="afk-status">● AFK MODE ACTIVE</div>
      <div class="afk-meta">
        <span>started: {fmt(afkFlag.metadata?.started_at)}</span>
        <span>expires: {fmt(afkFlag.metadata?.expires_at)}</span>
        <span>duration: {afkFlag.metadata?.duration_hours}h</span>
      </div>
      {#if afkFlag.metadata?.note}
        <div class="afk-note">note: {afkFlag.metadata.note}</div>
      {/if}
    </div>
  {:else}
    <div class="afk off">
      <div class="afk-status">○ AFK mode is OFF</div>
      <div class="muted">
        Toggle on via CLI: <code>memex afk on --for 4h --note "executing plan X"</code>
      </div>
    </div>
  {/if}

  <h2>Hard-deny rules ({hardDeny.length} user-defined)</h2>
  <div class="muted small">
    Always blocked, even in AFK mode. Built-in patterns also apply:
    rm -rf /, force-push to main, DROP DATABASE, kubectl delete on prod,
    --no-verify commits.
  </div>
  {#if hardDeny.length === 0}
    <p class="muted">no user-defined hard-deny rules</p>
  {:else}
    <div class="rule-list">
      {#each hardDeny as r}
        <div class="rule deny">
          <span class="badge">DENY</span>
          <div class="rule-body">
            <div class="rule-name">{r.name}</div>
            <div class="rule-args">{fmtArgs(r.metadata?.args_match)}</div>
            <div class="rule-reason">{r.metadata?.reason ?? r.description}</div>
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <h2>Approval policies ({approvalPolicies.length})</h2>
  <div class="muted small">
    User-trusted patterns memex auto-approves without prompting.
    AFK mode supplements these — it auto-approves anything that's not hard-denied.
  </div>
  {#if approvalPolicies.length === 0}
    <p class="muted">no approval policies — every tool prompt goes to you</p>
  {:else}
    <div class="rule-list">
      {#each approvalPolicies as r}
        <div class="rule" class:approve={r.metadata?.decision === "approve"}
             class:deny={r.metadata?.decision === "deny"}>
          <span class="badge">{r.metadata?.decision?.toUpperCase() ?? "?"}</span>
          <div class="rule-body">
            <div class="rule-name">
              <code>{r.metadata?.tool_pattern ?? "*"}</code>
            </div>
            <div class="rule-args">{fmtArgs(r.metadata?.args_match)}</div>
            <div class="rule-reason">{r.metadata?.reason ?? r.description}</div>
            <div class="rule-meta">
              priority {r.metadata?.priority ?? 100} ·
              {r.metadata?.enabled === false ? "disabled" : "enabled"}
            </div>
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <h2>Other constraints ({otherConstraints.length})</h2>
  <div class="muted small">
    User feedback rules + design constraints captured by memex
    across sessions. These are referenced via recall, not enforced
    by the PreToolUse gate.
  </div>
  {#if otherConstraints.length === 0}
    <p class="muted">none</p>
  {:else}
    <div class="rule-list">
      {#each otherConstraints.slice(0, 30) as r}
        <div class="rule">
          <span class="badge soft">RULE</span>
          <div class="rule-body">
            <div class="rule-name">{r.name}</div>
            <div class="rule-reason">{(r.description ?? "").split("\n")[0]}</div>
          </div>
        </div>
      {/each}
    </div>
  {/if}
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
    margin: 22px 0 4px;
    font-size: 12px;
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
  .small {
    font-size: 11px;
    margin-bottom: 8px;
    line-height: 1.4;
  }
  code {
    font-family: "Fira Code", monospace;
    font-size: 11px;
    background: #f3f4f6;
    padding: 1px 6px;
    border-radius: 3px;
  }

  .afk {
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 8px;
  }
  .afk.on {
    background: #fef3c7;
    border: 1px solid #fde047;
  }
  .afk.off {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
  }
  .afk-status {
    font-weight: 700;
    font-size: 14px;
    color: #1f2328;
    margin-bottom: 4px;
  }
  .afk.on .afk-status {
    color: #b45309;
  }
  .afk-meta {
    display: flex;
    gap: 12px;
    font-size: 11px;
    color: #57606a;
  }
  .afk-note {
    margin-top: 6px;
    font-size: 12px;
    color: #1f2328;
    font-style: italic;
  }

  .rule-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 4px;
  }
  .rule {
    display: grid;
    grid-template-columns: 70px 1fr;
    gap: 10px;
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 8px 12px;
  }
  .rule.deny {
    border-color: #fecaca;
    background: #fef2f2;
  }
  .rule.approve {
    border-color: #bbf7d0;
    background: #f0fdf4;
  }
  .badge {
    background: #6b7280;
    color: #ffffff;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 9px;
    text-align: center;
    align-self: flex-start;
  }
  .rule.deny .badge {
    background: #dc2626;
  }
  .rule.approve .badge {
    background: #16a34a;
  }
  .badge.soft {
    background: #cbd5e1;
    color: #1f2328;
  }
  .rule-name {
    font-size: 12px;
    font-weight: 600;
    color: #1f2328;
  }
  .rule-args {
    font-size: 11px;
    color: #57606a;
    font-family: "Fira Code", monospace;
    margin-top: 2px;
  }
  .rule-reason {
    font-size: 11px;
    color: #4b5563;
    margin-top: 4px;
    line-height: 1.4;
  }
  .rule-meta {
    font-size: 10px;
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
