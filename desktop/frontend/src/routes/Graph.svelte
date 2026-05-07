<script lang="ts">
  import { onMount, onDestroy } from "svelte";

  export let GetConcepts: (kind: string, limit: number, offset: number) => Promise<any>;
  export let GetEdgesFor: (id: string) => Promise<any>;

  let container: HTMLDivElement;
  let cy: any = null;
  let loading = true;
  let error: string | null = null;
  let kindFilter = "";
  let conceptCount = 0;
  let edgeCount = 0;

  // Color per concept kind. Picked to match the dark theme + give each
  // kind a recognisable hue.
  const KIND_COLOR: Record<string, string> = {
    decision: "#4f7dff",
    constraint: "#f5c878",
    fact: "#9ea4b3",
    pattern: "#b58aff",
    person: "#7adfd0",
    opinion: "#9ea4b3",
    question: "#ffa726",
    rejected: "#7c8190",
    approach: "#6dd58e",
    module: "#cc7eff",
    endpoint: "#74a5ff",
    task: "#2da46d",
    milestone: "#4ec495",
    project: "#a07cff",
    source: "#ff7070",
    file: "#9bb5e8",
    symbol: "#74a5ff",
  };
  const DEFAULT_COLOR = "#5a6175";

  function colorForKind(kind: string): string {
    return KIND_COLOR[kind] ?? DEFAULT_COLOR;
  }

  async function load() {
    loading = true;
    error = null;
    try {
      // Pull all concepts (capped at 1000 for the initial render).
      const res = await GetConcepts(kindFilter, 1000, 0);
      const concepts = res.concepts ?? [];
      conceptCount = concepts.length;

      // Pull edges per concept and dedup. For 100s of concepts this is
      // O(N) HTTP — fine over localhost; future: server-side /edges-bulk.
      const edgeKey = (e: any) => `${e.from_id}|${e.to_id}|${e.kind}`;
      const seen = new Map<string, any>();
      // Limit fan-out to keep first paint fast.
      for (const c of concepts.slice(0, 250)) {
        try {
          const er = await GetEdgesFor(c.id);
          for (const e of er.edges ?? []) seen.set(edgeKey(e), e);
        } catch {
          // skip
        }
      }
      const edges = Array.from(seen.values());
      edgeCount = edges.length;

      // Lazy-load Cytoscape from the bundle once; reuse the instance.
      const cytoscape = (await import("cytoscape")).default;
      if (cy) cy.destroy();
      cy = cytoscape({
        container,
        elements: [
          ...concepts.map((c: any) => ({
            data: {
              id: c.id,
              label: c.name?.length > 30 ? c.name.slice(0, 28) + "…" : c.name,
              kind: c.kind,
              fullName: c.name,
              description: c.description,
            },
          })),
          ...edges
            .filter((e) => seen.size === seen.size) // typescript noise
            .map((e: any) => ({
              data: {
                id: edgeKey(e),
                source: e.from_id,
                target: e.to_id,
                kind: e.kind,
              },
            })),
        ],
        style: [
          {
            selector: "node",
            style: {
              "background-color": "data(kind)",
              "background-color-mapped": (ele: any) =>
                colorForKind(ele.data("kind")),
              label: "data(label)",
              color: "#aab1bd",
              "font-size": 9,
              "text-margin-y": -4,
              width: 14,
              height: 14,
              "border-width": 0,
            },
          },
          {
            selector: "node",
            style: {
              "background-color": (ele: any) => colorForKind(ele.data("kind")),
            },
          },
          {
            selector: "edge",
            style: {
              "curve-style": "bezier",
              width: 1,
              "line-color": "#2a2e39",
              "target-arrow-color": "#2a2e39",
              "target-arrow-shape": "triangle",
              "arrow-scale": 0.7,
              opacity: 0.7,
            },
          },
          {
            selector: "edge[kind = 'same_as']",
            style: {
              "line-style": "dashed",
              "line-color": "#5b6580",
            },
          },
          {
            selector: "edge[kind = 'calls']",
            style: { "line-color": "#3a4154" },
          },
          {
            selector: "edge[kind = 'defined_in']",
            style: { "line-color": "#2e3548" },
          },
          {
            selector: ":selected",
            style: {
              "border-width": 2,
              "border-color": "#74a5ff",
              "border-opacity": 1,
            },
          },
        ],
        layout: {
          name: "cose",
          animate: false,
          nodeRepulsion: 8000,
          idealEdgeLength: 80,
          padding: 20,
        },
        wheelSensitivity: 0.2,
      });

      cy.on("tap", "node", (evt: any) => {
        const n = evt.target;
        selected = {
          id: n.data("id"),
          name: n.data("fullName"),
          kind: n.data("kind"),
          description: n.data("description"),
        };
      });
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  let selected: any = null;

  onMount(() => load());
  onDestroy(() => {
    if (cy) cy.destroy();
  });

  const kindOptions = [
    "", "decision", "constraint", "fact", "pattern", "approach",
    "task", "project", "module", "endpoint",
    "source", "file", "symbol",
  ];
</script>

<header>
  <h1>Graph</h1>
  <div class="controls">
    <select bind:value={kindFilter} on:change={() => load()}>
      {#each kindOptions as k}
        <option value={k}>{k || "(all kinds)"}</option>
      {/each}
    </select>
    <button on:click={() => load()}>refresh</button>
    <span class="muted">
      {#if !loading}
        {conceptCount} nodes · {edgeCount} edges
      {/if}
    </span>
  </div>
</header>

<div class="graph-wrap">
  <div class="canvas" bind:this={container}></div>
  {#if loading}
    <div class="overlay"><span>building graph…</span></div>
  {:else if error}
    <div class="overlay"><span class="error">error: {error}</span></div>
  {/if}
  {#if selected}
    <aside class="detail">
      <button class="close" on:click={() => (selected = null)}>×</button>
      <div class="kind-chip" style="background: {colorForKind(selected.kind)}22; color: {colorForKind(selected.kind)}">
        {selected.kind}
      </div>
      <h3>{selected.name}</h3>
      <pre>{selected.description}</pre>
      <div class="muted">id: {selected.id}</div>
    </aside>
  {/if}
</div>

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
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 12px;
  }
  select, button {
    background: #1c1f26;
    color: #e6e9ef;
    border: 1px solid #2a2e39;
    padding: 5px 10px;
    border-radius: 4px;
    font-size: 12px;
    cursor: pointer;
  }
  .graph-wrap {
    position: relative;
    width: 100%;
    height: calc(100vh - 130px);
    background: #0c0e12;
    border: 1px solid #232631;
    border-radius: 6px;
    overflow: hidden;
  }
  .canvas {
    width: 100%;
    height: 100%;
  }
  .overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(15, 17, 21, 0.6);
    color: #6f7382;
    font-size: 13px;
  }
  .error {
    color: #ff8a80;
  }
  .detail {
    position: absolute;
    right: 12px;
    top: 12px;
    width: 320px;
    max-height: 60%;
    background: #15171c;
    border: 1px solid #2a2e39;
    border-radius: 8px;
    padding: 14px 16px 18px;
    overflow-y: auto;
  }
  .detail h3 {
    margin: 8px 0;
    font-size: 14px;
    font-weight: 600;
    color: #ffffff;
  }
  .detail pre {
    font-family: -apple-system, "Segoe UI", sans-serif;
    font-size: 12px;
    color: #b9bfcc;
    white-space: pre-wrap;
    word-wrap: break-word;
    margin: 0 0 10px;
    line-height: 1.4;
  }
  .kind-chip {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-weight: 600;
  }
  .close {
    position: absolute;
    top: 6px;
    right: 6px;
    background: transparent;
    border: 0;
    color: #6f7382;
    font-size: 18px;
    cursor: pointer;
  }
  .muted {
    color: #6f7382;
  }
</style>
