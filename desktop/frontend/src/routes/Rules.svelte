<script lang="ts">
  import { onMount } from "svelte";
  import {
    EnableAFK,
    DisableAFK,
    AFKStatus,
  } from "../../wailsjs/go/main/App.js";

  export let GetConcepts: (kind: string, limit: number, offset: number) => Promise<any>;

  // AFK toggle state
  let afk: any = null;
  let afkBusy = false;
  let durationHours = 4;
  let note = "";

  // Add-rule form state
  let showAddRule = false;
  let ruleType: "approval_approve" | "approval_deny" | "hard_deny" | "constraint" = "approval_approve";
  let ruleToolPattern = "Bash";
  let ruleArgsJson = '{"command": {"prefix": "git status"}}';
  let ruleReason = "";
  let ruleBusy = false;
  let ruleError = "";
  // Hardcoded placeholder for the args JSON textarea — kept as a JS
  // string so Svelte's parser doesn't see the curly braces as expression
  // delimiters in the placeholder attribute.
  const argsPlaceholder = '{"command": {"prefix": "git status"}}';

  async function submitRule() {
    if (!ruleReason.trim() && ruleType !== "constraint") {
      ruleError = "reason is required";
      return;
    }
    ruleBusy = true;
    ruleError = "";
    try {
      const { CreateConcept, PatchNode } = await import("../../wailsjs/go/main/App.js");
      let kind: string;
      let metadataPatch: Record<string, any> = {};
      let name: string;

      if (ruleType === "constraint") {
        kind = "constraint";
        name = ruleReason || "user constraint";
      } else {
        kind = "constraint";
        let argsObj: any;
        try {
          argsObj = JSON.parse(ruleArgsJson || "{}");
        } catch (e: any) {
          ruleError = `invalid args JSON: ${e.message}`;
          return;
        }
        const decision =
          ruleType === "approval_approve" ? "approve"
          : ruleType === "approval_deny"  ? "deny"
          : "deny";
        const policyType = ruleType === "hard_deny" ? "hard_deny" : "approval";
        metadataPatch = {
          policy_type: policyType,
          tool_pattern: ruleToolPattern,
          args_match: argsObj,
          decision: decision,
          reason: ruleReason,
          enabled: true,
          priority: 100,
        };
        name = `${decision}: ${ruleToolPattern} ${JSON.stringify(argsObj).slice(0, 40)}`;
      }
      const created = await CreateConcept(name, ruleReason, kind);
      if (created?.error) { ruleError = created.error; return; }
      if (Object.keys(metadataPatch).length) {
        await PatchNode(created.id, { metadata_patch: metadataPatch });
      }
      showAddRule = false;
      ruleReason = "";
      await load();
    } catch (e: any) {
      ruleError = String(e?.message || e);
    } finally {
      ruleBusy = false;
    }
  }

  async function refreshAfk() {
    try {
      afk = await AFKStatus();
    } catch (e: any) {
      // tolerate
    }
  }

  async function turnOnAfk() {
    afkBusy = true;
    try {
      await EnableAFK(durationHours, note);
      await refreshAfk();
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      afkBusy = false;
    }
  }

  async function turnOffAfk() {
    afkBusy = true;
    try {
      await DisableAFK();
      await refreshAfk();
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      afkBusy = false;
    }
  }

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

  onMount(() => { load(); refreshAfk(); });
</script>

<header>
  <h1>Rules &amp; AFK</h1>
  <div class="header-actions">
    <button class="primary" on:click={() => (showAddRule = true)}>+ Add rule</button>
    <button on:click={() => { load(); refreshAfk(); }}>refresh</button>
  </div>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else}
  <h2>AFK mode</h2>
  {#if afk?.active && afk?.status}
    <div class="afk on">
      <div class="afk-row">
        <div class="afk-status">● AFK MODE ACTIVE</div>
        <button class="off-btn" disabled={afkBusy} on:click={turnOffAfk}>
          {afkBusy ? "..." : "Turn off"}
        </button>
      </div>
      <div class="afk-meta">
        <span>started: {fmt(afk.status.started_at)}</span>
        <span>expires: {fmt(afk.status.expires_at)}</span>
        <span>duration: {afk.status.duration_hours}h</span>
      </div>
      {#if afk.status.note}
        <div class="afk-note">note: <em>{afk.status.note}</em></div>
      {/if}
      <div class="muted small">
        Auto-approves every PreToolUse-gated call EXCEPT hard-deny patterns.
        Every approve/deny is audit-logged in the Activity tab.
      </div>
    </div>
  {:else}
    <div class="afk off">
      <div class="afk-status">○ AFK mode is OFF</div>
      <div class="muted small">
        Turn on to delegate "work the plan unattended; I'll review on return."
      </div>
      <div class="afk-form">
        <label>
          duration (hours)
          <input type="number" min="0.1" step="0.5" bind:value={durationHours} disabled={afkBusy}/>
        </label>
        <label class="grow">
          note
          <input type="text" placeholder="executing plan X — review on return"
                 bind:value={note} disabled={afkBusy}/>
        </label>
        <button class="primary" disabled={afkBusy} on:click={turnOnAfk}>
          {afkBusy ? "..." : "Turn on"}
        </button>
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

{#if showAddRule}
  <div class="modal-bg" on:click={() => (showAddRule = false)} role="dialog">
    <div class="modal" on:click|stopPropagation>
      <h3>Add rule</h3>
      <p class="muted small">
        Approval policies + hard-deny patterns are consulted on every
        PreToolUse hook fire. Constraints are recall-surfaced reminders
        for the AI (not enforced).
      </p>
      <label>
        Type
        <select bind:value={ruleType}>
          <option value="approval_approve">Auto-approve (PreToolUse)</option>
          <option value="approval_deny">Auto-deny (PreToolUse)</option>
          <option value="hard_deny">Hard-deny (bypasses AFK)</option>
          <option value="constraint">Constraint (recall-only, not enforced)</option>
        </select>
      </label>
      {#if ruleType !== "constraint"}
        <label>
          Tool pattern (regex)
          <input type="text" placeholder="Bash | Edit|Write|MultiEdit | mcp__memex__*"
                 bind:value={ruleToolPattern} disabled={ruleBusy} />
        </label>
        <label>
          Args matcher (JSON)
          <textarea rows="3" placeholder={argsPlaceholder}
                    bind:value={ruleArgsJson} disabled={ruleBusy}></textarea>
        </label>
      {/if}
      <label>
        Reason / description
        <textarea rows="2"
                  placeholder="why this rule — surfaced when memex applies it"
                  bind:value={ruleReason} disabled={ruleBusy}></textarea>
      </label>
      {#if ruleError}
        <div class="form-error">{ruleError}</div>
      {/if}
      <div class="modal-actions">
        <button on:click={() => (showAddRule = false)} disabled={ruleBusy}>cancel</button>
        <button class="primary" disabled={ruleBusy} on:click={submitRule}>
          {ruleBusy ? "saving…" : "save rule"}
        </button>
      </div>
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
  .afk-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .afk-meta {
    display: flex;
    gap: 12px;
    font-size: 11px;
    color: #57606a;
    margin-top: 4px;
  }
  .afk-note {
    margin-top: 6px;
    font-size: 12px;
    color: #1f2328;
  }
  .afk-form {
    display: flex;
    align-items: flex-end;
    gap: 8px;
    margin-top: 10px;
  }
  .afk-form label {
    display: flex;
    flex-direction: column;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #6b7280;
    font-weight: 600;
    flex-shrink: 0;
  }
  .afk-form label.grow {
    flex: 1;
  }
  .afk-form input {
    margin-top: 3px;
    padding: 6px 9px;
    border: 1px solid #d0d7de;
    border-radius: 4px;
    font-family: inherit;
    font-size: 13px;
  }
  .afk-form input[type="number"] {
    width: 80px;
  }
  .off-btn {
    background: #ffffff;
    border: 1px solid #d0d7de;
    padding: 5px 12px;
    border-radius: 5px;
    font-size: 12px;
    cursor: pointer;
    font-family: inherit;
  }
  .off-btn:hover {
    background: #fee2e2;
    border-color: #fecaca;
    color: #991b1b;
  }
  .header-actions {
    display: flex;
    gap: 8px;
  }
  button.primary {
    background: #fef3c7;
    border-color: #fde047;
    font-weight: 600;
  }
  button.primary:hover:not(:disabled) {
    background: #fde047;
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
    padding: 22px 26px;
    width: 480px;
    max-height: 90vh;
    overflow-y: auto;
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
  .modal input, .modal select, .modal textarea {
    display: block;
    width: 100%;
    margin-top: 4px;
    padding: 7px 10px;
    border: 1px solid #d0d7de;
    border-radius: 5px;
    font-family: inherit;
    font-size: 13px;
  }
  .modal textarea {
    font-family: "Fira Code", monospace;
    font-size: 12px;
    resize: vertical;
  }
  .modal-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 18px;
  }
  .form-error {
    margin-top: 10px;
    color: #dc2626;
    font-size: 12px;
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
