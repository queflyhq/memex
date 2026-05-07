<script lang="ts">
  import { onMount } from "svelte";
  import {
    GetDaemonStatus,
    GetStats,
    GetConcepts,
    GetEdgesFor,
    GetTasks,
    GetSkills,
  } from "../wailsjs/go/main/App.js";
  import qfBadge from "./assets/quefly-badge.svg";
  import Dashboard from "./routes/Dashboard.svelte";
  import Graph from "./routes/Graph.svelte";
  import Concepts from "./routes/Concepts.svelte";
  import Activity from "./routes/Activity.svelte";
  import Tasks from "./routes/Tasks.svelte";
  import Skills from "./routes/Skills.svelte";
  import Database from "./routes/Database.svelte";
  import Sources from "./routes/Sources.svelte";
  import Rules from "./routes/Rules.svelte";
  import Secrets from "./routes/Secrets.svelte";
  import Integrations from "./routes/Integrations.svelte";

  type Tab =
    | "dashboard"
    | "activity"
    | "graph"
    | "concepts"
    | "tasks"
    | "rules"
    | "sources"
    | "skills"
    | "integrations"
    | "secrets"
    | "database";
  let active: Tab = "dashboard";
  let daemonAlive = false;
  let daemonURL = "";

  onMount(async () => {
    const status = await GetDaemonStatus();
    daemonAlive = !!status.alive;
    daemonURL = String(status.url || "");
  });

  const groups: { label: string; tabs: { id: Tab; label: string; emoji: string }[] }[] = [
    {
      label: "Overview",
      tabs: [
        { id: "dashboard", label: "Dashboard", emoji: "⌂" },
        { id: "activity",  label: "Activity",  emoji: "⌚" },
      ],
    },
    {
      label: "Knowledge",
      tabs: [
        // Graph hidden until focal-mode rewrite ships — current force-
        // directed view of 8.8k nodes wasn't legible. Concepts page
        // (with search + facets + detail panel) covers the primary
        // browse use case for now.
        { id: "concepts", label: "Concepts",         emoji: "☷" },
        { id: "tasks",    label: "Tasks & Projects", emoji: "☑" },
        { id: "rules",    label: "Rules & AFK",      emoji: "⛨" },
      ],
    },
    {
      label: "Code",
      tabs: [
        { id: "sources",  label: "Sources",  emoji: "▸" },
      ],
    },
    {
      label: "Tooling",
      tabs: [
        { id: "skills",        label: "Skills",       emoji: "✦" },
        { id: "integrations",  label: "Integrations", emoji: "🔗" },
        { id: "secrets",       label: "Secrets",      emoji: "🔒" },
      ],
    },
    {
      label: "Power user",
      tabs: [
        { id: "database", label: "Database", emoji: "▤" },
      ],
    },
  ];
</script>

<main class="layout">
  <aside class="sidebar">
    <div class="brand">
      <img src={qfBadge} alt="Quefly" class="badge" />
      <span class="brand-name">memeX</span>
    </div>
    <nav>
      {#each groups as g}
        <div class="nav-group-label">{g.label}</div>
        {#each g.tabs as t}
          <button
            class:active={active === t.id}
            on:click={() => (active = t.id)}
          >
            <span class="ico">{t.emoji}</span>
            <span>{t.label}</span>
          </button>
        {/each}
      {/each}
    </nav>
    <div class="status">
      <span class="dot" class:alive={daemonAlive}></span>
      <span class="muted">
        daemon: {daemonAlive ? "alive" : "down"}
      </span>
    </div>
  </aside>

  <section class="main">
    {#if active === "dashboard"}
      <Dashboard {GetStats} />
    {:else if active === "graph"}
      <Graph {GetConcepts} {GetEdgesFor} />
    {:else if active === "concepts"}
      <Concepts {GetConcepts} />
    {:else if active === "sources"}
      <Sources />
    {:else if active === "tasks"}
      <Tasks {GetTasks} />
    {:else if active === "rules"}
      <Rules {GetConcepts} />
    {:else if active === "skills"}
      <Skills {GetSkills} />
    {:else if active === "integrations"}
      <Integrations />
    {:else if active === "secrets"}
      <Secrets />
    {:else if active === "database"}
      <Database />
    {:else if active === "activity"}
      <Activity />
    {/if}
  </section>
</main>

<style>
  :global(html, body) {
    margin: 0;
    padding: 0;
    height: 100vh;
    background: #ffffff;
    color: #1f2328;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
  }
  :global(*, *::before, *::after) {
    box-sizing: border-box;
  }

  .layout {
    display: grid;
    grid-template-columns: 240px 1fr;
    height: 100vh;
  }
  .sidebar {
    background: #fafbfc;
    border-right: 1px solid #e6e8eb;
    display: flex;
    flex-direction: column;
    padding: 18px 14px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 28px;
    padding: 4px 6px;
  }
  .badge {
    width: 44px;
    height: 44px;
    object-fit: contain;
    flex-shrink: 0;
  }
  .brand-name {
    font-family: "Plus Jakarta Sans", -apple-system, BlinkMacSystemFont,
      "Segoe UI", sans-serif;
    font-size: 22px;
    font-weight: 800;
    color: #1f2328;
    letter-spacing: -0.03em;
    line-height: 1;
  }
  nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
    overflow-y: auto;
  }
  .nav-group-label {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.7px;
    color: #9ca3af;
    font-weight: 700;
    padding: 14px 12px 4px;
  }
  .nav-group-label:first-child {
    padding-top: 0;
  }
  nav button {
    background: transparent;
    border: 0;
    color: #57606a;
    text-align: left;
    padding: 9px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 14px;
    font-family: inherit;
    display: flex;
    align-items: center;
    gap: 12px;
    transition: background 0.1s ease;
  }
  nav button:hover {
    background: #eef0f2;
    color: #1f2328;
  }
  nav button.active {
    background: #fef3c7;
    color: #1f2328;
    font-weight: 600;
  }
  .ico {
    width: 14px;
    text-align: center;
    color: #8b96a3;
  }
  nav button.active .ico {
    color: #b45309;
  }
  .status {
    font-size: 12px;
    color: #6b7280;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 6px 0;
    border-top: 1px solid #e6e8eb;
    margin-top: 8px;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #c0392b;
  }
  .dot.alive {
    background: #16a34a;
  }
  .muted {
    color: #6b7280;
  }
  .main {
    overflow-y: auto;
    padding: 24px 32px;
    background: #ffffff;
  }
</style>
