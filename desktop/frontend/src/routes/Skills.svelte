<script lang="ts">
  import { onMount } from "svelte";
  import { GetStats, InstallSkill } from "../../wailsjs/go/main/App.js";

  export let GetSkills: () => Promise<any>;

  let skills: any[] = [];
  let validatedSkills: string[] = [];
  let validationsCount = 0;
  let stats: any = null;
  let loading = true;
  let error: string | null = null;
  let installName = "";
  let installing = false;
  let installMsg = "";

  async function installNew() {
    if (!installName.trim()) return;
    installing = true;
    installMsg = "";
    error = null;
    try {
      const res = await InstallSkill(installName.trim());
      if (res?.error) {
        error = res.error;
      } else {
        installMsg = `installed ${installName} (${res?.concepts ?? "?"} concepts)`;
        installName = "";
        await load();
      }
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      installing = false;
    }
  }

  async function load() {
    loading = true;
    error = null;
    try {
      const skillsRes = await GetSkills();
      skills = skillsRes?.skills ?? [];
      validatedSkills = skillsRes?.validated_skills ?? [];
      validationsCount = skillsRes?.validations_count ?? 0;
      try {
        stats = await GetStats(0);
      } catch {
        // older daemon — fine
      }
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  onMount(() => load());

  $: validatedSet = new Set(validatedSkills);
</script>

<header>
  <h1>Skills &amp; Embeddings</h1>
  <button on:click={() => load()}>refresh</button>
</header>

{#if loading}
  <p class="muted">loading…</p>
{:else if error}
  <p class="error">error: {error}</p>
{:else}
  <h2>Embedding tier</h2>
  <div class="grid">
    <div class="tile">
      <div class="value">
        {#if stats}
          {stats.embed_tier_available ? "✓ available" : "× missing"}
        {:else}
          —
        {/if}
      </div>
      <div class="label">Embedding provider</div>
      <div class="hint">
        {#if stats?.embed_model}
          {stats.embed_model}
        {:else}
          BAAI/bge-small-en-v1.5 (default, 33M params)
        {/if}
      </div>
    </div>
    <div class="tile">
      <div class="value">{stats?.vectors_total ?? "—"}</div>
      <div class="label">Vectors indexed</div>
      <div class="hint">one per concept (symbols + decisions + tasks…)</div>
    </div>
    <div class="tile">
      <div class="value">{stats?.vector_dim ?? "384"}</div>
      <div class="label">Vector dimension</div>
      <div class="hint">cosine distance via DuckDB VSS</div>
    </div>
  </div>

  <h2>Install skill</h2>
  <div class="install-row">
    <input
      type="text"
      placeholder="skill name (e.g. core-validations, project-management, using-memex)"
      bind:value={installName}
      disabled={installing}
    />
    <button class="primary" on:click={installNew} disabled={installing || !installName.trim()}>
      {installing ? "installing…" : "+ install"}
    </button>
  </div>
  {#if installMsg}<div class="banner-ok">✓ {installMsg}</div>{/if}

  <h2>Skill catalogue ({skills.length})</h2>
  <div class="skill-list">
    {#each skills as s}
      {@const validated = validatedSet.has(s.name)}
      <div class="skill-card" class:validated>
        <div class="skill-head">
          <span class="skill-name">{s.name}</span>
          {#if validated}
            <span class="chip chip-validated">validated</span>
          {/if}
          {#if s.version}
            <span class="chip">v{s.version}</span>
          {/if}
        </div>
        {#if s.description}
          <div class="skill-desc">{s.description}</div>
        {/if}
      </div>
    {/each}
  </div>

  <h2>Validations summary</h2>
  <div class="grid grid-2">
    <div class="tile">
      <div class="value">{validationsCount}</div>
      <div class="label">Total validations fired</div>
      <div class="hint">across all sessions in the episodic stream</div>
    </div>
    <div class="tile">
      <div class="value">{validatedSkills.length}</div>
      <div class="label">Distinct skills used</div>
      <div class="hint">{validatedSkills.join(", ") || "none yet"}</div>
    </div>
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
  .grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
  }
  .grid-2 {
    grid-template-columns: repeat(2, 1fr);
  }
  .tile {
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 14px 16px;
  }
  .value {
    font-size: 22px;
    font-weight: 700;
    color: #1f2328;
  }
  .label {
    font-size: 12px;
    color: #1f2328;
    margin-top: 3px;
    font-weight: 500;
  }
  .hint {
    font-size: 11px;
    color: #6b7280;
    margin-top: 3px;
    line-height: 1.4;
  }
  .skill-list {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 8px;
  }
  .skill-card {
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 12px 14px;
  }
  .skill-card.validated {
    border-color: #fde047;
    background: #fefce8;
  }
  .skill-head {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;
  }
  .skill-name {
    font-size: 13px;
    font-weight: 600;
    color: #1f2328;
  }
  .skill-desc {
    font-size: 12px;
    color: #57606a;
    line-height: 1.4;
  }
  .chip {
    background: #eef0f2;
    color: #57606a;
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 9px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    font-weight: 600;
  }
  .chip-validated {
    background: #fef3c7;
    color: #b45309;
  }
  .install-row {
    display: flex;
    gap: 8px;
    margin-bottom: 6px;
  }
  .install-row input {
    flex: 1;
    padding: 7px 10px;
    border: 1px solid #d0d7de;
    border-radius: 5px;
    font-family: inherit;
    font-size: 13px;
  }
  .install-row button.primary {
    background: #fef3c7;
    border-color: #fde047;
    font-weight: 600;
  }
  .banner-ok {
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    color: #166534;
    border-radius: 6px;
    padding: 8px 12px;
    margin-bottom: 12px;
    font-size: 13px;
  }
  .muted {
    color: #6b7280;
  }
  .error {
    color: #dc2626;
  }
</style>
