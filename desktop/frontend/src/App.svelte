<script lang="ts">
  import { onMount } from "svelte";
  import {
    GetDaemonStatus,
    GetStats,
    GetConcepts,
    GetEdgesFor,
  } from "../wailsjs/go/main/App.js";
  import qfBadge from "./assets/quefly-badge.svg";
  import Dashboard from "./routes/Dashboard.svelte";
  import Graph from "./routes/Graph.svelte";
  import Concepts from "./routes/Concepts.svelte";
  import Activity from "./routes/Activity.svelte";

  type Tab = "dashboard" | "graph" | "concepts" | "activity";
  let active: Tab = "dashboard";
  let daemonAlive = false;
  let daemonURL = "";

  onMount(async () => {
    const status = await GetDaemonStatus();
    daemonAlive = !!status.alive;
    daemonURL = String(status.url || "");
  });

  const tabs: { id: Tab; label: string; emoji: string }[] = [
    { id: "dashboard", label: "Dashboard", emoji: "⌂" },
    { id: "graph", label: "Graph", emoji: "◫" },
    { id: "concepts", label: "Concepts", emoji: "☷" },
    { id: "activity", label: "Activity", emoji: "⌚" },
  ];
</script>

<main class="layout">
  <aside class="sidebar">
    <div class="brand">
      <img src={qfBadge} alt="Quefly" class="badge" />
      <span>memex</span>
    </div>
    <nav>
      {#each tabs as t}
        <button
          class:active={active === t.id}
          on:click={() => (active = t.id)}
        >
          <span class="ico">{t.emoji}</span>
          <span>{t.label}</span>
        </button>
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
    background: #0f1115;
    color: #e6e9ef;
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
    grid-template-columns: 220px 1fr;
    height: 100vh;
  }
  .sidebar {
    background: #15171c;
    border-right: 1px solid #232631;
    display: flex;
    flex-direction: column;
    padding: 18px 14px;
  }
  .brand {
    font-size: 18px;
    font-weight: 600;
    letter-spacing: 0.5px;
    margin-bottom: 22px;
    color: #d3d8e1;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .badge {
    width: 24px;
    height: 24px;
  }
  nav {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
  }
  nav button {
    background: transparent;
    border: 0;
    color: #aab1bd;
    text-align: left;
    padding: 9px 11px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 14px;
    display: flex;
    align-items: center;
    gap: 10px;
    transition: background 0.1s ease;
  }
  nav button:hover {
    background: #1c1f26;
    color: #e6e9ef;
  }
  nav button.active {
    background: #232936;
    color: #ffffff;
  }
  .ico {
    width: 14px;
    text-align: center;
    color: #6f7382;
  }
  nav button.active .ico {
    color: #74a5ff;
  }
  .status {
    font-size: 12px;
    color: #6f7382;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 6px 0;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #6c2530;
  }
  .dot.alive {
    background: #2da46d;
  }
  .muted {
    color: #6f7382;
  }
  .main {
    overflow-y: auto;
    padding: 24px 32px;
  }
</style>
