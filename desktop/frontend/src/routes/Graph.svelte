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
  let selected: any = null;

  // Quefly-themed palette — yellows, blacks, slate-grays for the
  // white background. Each kind gets a distinct hue while keeping
  // overall harmony with the brand.
  const KIND_COLOR: Record<string, string> = {
    decision:   "#fbbf24",  // Quefly yellow — primary attention
    constraint: "#f97316",  // amber/orange (rules)
    fact:       "#64748b",  // slate
    pattern:    "#a855f7",  // purple
    person:     "#06b6d4",  // teal
    opinion:    "#94a3b8",  // light slate
    question:   "#ec4899",  // pink (open questions)
    rejected:   "#cbd5e1",  // pale (de-emphasized)
    approach:   "#16a34a",  // green (validation)
    module:     "#8b5cf6",  // violet (architecture)
    endpoint:   "#0ea5e9",  // sky (API surface)
    task:       "#22c55e",  // green (active work)
    milestone:  "#15803d",  // dark green
    project:    "#7c3aed",  // dark violet
    source:     "#dc2626",  // red (codebase)
    file:       "#3b82f6",  // blue
    symbol:     "#1f2328",  // black (Quefly brand)
  };
  const DEFAULT_COLOR = "#94a3b8";

  function colorForKind(kind: string): string {
    return KIND_COLOR[kind] ?? DEFAULT_COLOR;
  }

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await GetConcepts(kindFilter, 500, 0);
      const concepts = res.concepts ?? [];
      conceptCount = concepts.length;

      // Pull edges per concept in parallel batches so first paint is fast.
      const edgeKey = (e: any) => `${e.from_id}|${e.to_id}|${e.kind}`;
      const seen = new Map<string, any>();
      const batchSize = 12;
      for (let i = 0; i < concepts.length; i += batchSize) {
        const batch = concepts.slice(i, i + batchSize);
        const results = await Promise.all(
          batch.map((c: any) =>
            GetEdgesFor(c.id).catch(() => ({ edges: [] }))
          ),
        );
        for (const er of results) {
          for (const e of (er?.edges ?? [])) seen.set(edgeKey(e), e);
        }
      }
      const edges = Array.from(seen.values());
      // Filter edges to only those connecting two concepts we loaded.
      const idSet = new Set(concepts.map((c: any) => c.id));
      const visibleEdges = edges.filter(
        (e: any) => idSet.has(e.from_id) && idSet.has(e.to_id),
      );
      edgeCount = visibleEdges.length;

      const cytoscape = (await import("cytoscape")).default;
      if (cy) cy.destroy();

      // Build per-kind selectors so node colours work without
      // function-style-values (which Cytoscape's static style refuses).
      const kindSelectors = Object.entries(KIND_COLOR).map(([k, c]) => ({
        selector: `node[kind = '${k}']`,
        style: { "background-color": c },
      }));

      cy = cytoscape({
        container,
        elements: [
          ...concepts.map((c: any) => ({
            data: {
              id: c.id,
              label: c.name?.length > 32 ? c.name.slice(0, 30) + "…" : c.name,
              kind: c.kind,
              fullName: c.name,
              description: c.description,
            },
          })),
          ...visibleEdges.map((e: any) => ({
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
              "background-color": DEFAULT_COLOR,
              label: "data(label)",
              color: "#1f2328",
              "font-family": '"Plus Jakarta Sans", "Inter", sans-serif',
              "font-size": 10,
              "font-weight": 500,
              "text-margin-y": -4,
              "text-valign": "top",
              "text-halign": "center",
              width: 16,
              height: 16,
              "border-width": 1,
              "border-color": "#ffffff",
              "border-opacity": 1,
              "min-zoomed-font-size": 8,
            },
          },
          ...kindSelectors,
          {
            selector: "edge",
            style: {
              "curve-style": "bezier",
              width: 1,
              "line-color": "#cbd5e1",
              "target-arrow-color": "#cbd5e1",
              "target-arrow-shape": "triangle",
              "arrow-scale": 0.7,
              opacity: 0.85,
            },
          },
          {
            selector: "edge[kind = 'same_as']",
            style: { "line-style": "dashed", "line-color": "#fbbf24",
                     "target-arrow-color": "#fbbf24", width: 1.5 },
          },
          {
            selector: "edge[kind = 'calls']",
            style: { "line-color": "#94a3b8", "target-arrow-color": "#94a3b8" },
          },
          {
            selector: "edge[kind = 'defined_in']",
            style: { "line-color": "#e5e7eb", "target-arrow-color": "#e5e7eb" },
          },
          {
            selector: "edge[kind = 'part_of']",
            style: { "line-color": "#e5e7eb", "target-arrow-color": "#e5e7eb" },
          },
          {
            selector: "edge[kind = 'motivated_by']",
            style: { "line-color": "#fbbf24", "target-arrow-color": "#fbbf24" },
          },
          {
            selector: "edge[kind = 'implements']",
            style: { "line-color": "#16a34a", "target-arrow-color": "#16a34a" },
          },
          {
            selector: "edge[kind = 'supersedes']",
            style: { "line-color": "#dc2626", "target-arrow-color": "#dc2626",
                     "line-style": "dashed" },
          },
          {
            selector: "node:selected",
            style: {
              "border-width": 3,
              "border-color": "#fbbf24",
              "border-opacity": 1,
            },
          },
          {
            selector: "edge:selected",
            style: {
              width: 2.5,
              "line-color": "#fbbf24",
              "target-arrow-color": "#fbbf24",
            },
          },
        ],
        layout: {
          name: "cose",
          animate: false,
          nodeRepulsion: 9000,
          idealEdgeLength: 90,
          padding: 30,
          fit: true,
          gravity: 1,
          numIter: 1000,
        },
        wheelSensitivity: 0.2,
        minZoom: 0.2,
        maxZoom: 3,
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
      cy.on("tap", (evt: any) => {
        if (evt.target === cy) selected = null;
      });
    } catch (e: any) {
      error = String(e?.message || e);
    } finally {
      loading = false;
    }
  }

  onMount(() => load());
  onDestroy(() => {
    if (cy) cy.destroy();
  });

  function fitToScreen() {
    if (cy) cy.fit(undefined, 30);
  }

  const kindOptions = [
    "", "decision", "constraint", "fact", "pattern", "approach",
    "task", "project", "module", "endpoint",
    "source", "file", "symbol", "person", "opinion", "question",
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
    <button on:click={fitToScreen}>fit</button>
    <span class="muted">
      {#if !loading}
        {conceptCount} nodes · {edgeCount} edges
      {/if}
    </span>
  </div>
</header>

<div class="legend">
  {#each Object.entries(KIND_COLOR) as [k, c]}
    <span class="chip">
      <span class="dot" style="background: {c}"></span>{k}
    </span>
  {/each}
</div>

<div class="graph-wrap">
  <div class="canvas" bind:this={container}></div>
  {#if loading}
    <div class="overlay"><span>building graph…</span></div>
  {:else if error}
    <div class="overlay"><span class="error">error: {error}</span></div>
  {:else if conceptCount === 0}
    <div class="overlay">
      <span class="muted">no concepts of kind '{kindFilter || "any"}'</span>
    </div>
  {/if}
  {#if selected}
    <aside class="detail">
      <button class="close" on:click={() => (selected = null)}>×</button>
      <div
        class="kind-chip"
        style="background: {colorForKind(selected.kind)}; color: white"
      >
        {selected.kind}
      </div>
      <h3>{selected.name}</h3>
      <pre>{selected.description ?? ""}</pre>
      <div class="muted">id: {selected.id}</div>
    </aside>
  {/if}
</div>

<style>
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }
  h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
    color: #1f2328;
    font-family: "Plus Jakarta Sans", -apple-system, sans-serif;
    letter-spacing: -0.02em;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
  }
  select, button {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    padding: 5px 10px;
    border-radius: 5px;
    font-size: 12px;
    cursor: pointer;
    font-family: inherit;
  }
  select:hover, button:hover {
    background: #f3f4f6;
  }
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    color: #57606a;
  }
  .chip .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .graph-wrap {
    position: relative;
    width: 100%;
    height: calc(100vh - 180px);
    background: #ffffff;
    border: 1px solid #e6e8eb;
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
    background: rgba(255, 255, 255, 0.85);
    color: #6b7280;
    font-size: 13px;
  }
  .error {
    color: #dc2626;
  }
  .detail {
    position: absolute;
    right: 12px;
    top: 12px;
    width: 320px;
    max-height: 70%;
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 8px;
    padding: 14px 16px 18px;
    overflow-y: auto;
    box-shadow: 0 4px 12px rgba(31, 35, 40, 0.10);
  }
  .detail h3 {
    margin: 8px 0;
    font-size: 14px;
    font-weight: 600;
    color: #1f2328;
    font-family: "Plus Jakarta Sans", -apple-system, sans-serif;
  }
  .detail pre {
    font-family: "Inter", -apple-system, sans-serif;
    font-size: 12px;
    color: #4b5563;
    white-space: pre-wrap;
    word-wrap: break-word;
    margin: 0 0 10px;
    line-height: 1.45;
  }
  .kind-chip {
    display: inline-block;
    padding: 2px 9px;
    border-radius: 10px;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-weight: 700;
  }
  .close {
    position: absolute;
    top: 6px;
    right: 6px;
    background: transparent;
    border: 0;
    color: #6b7280;
    font-size: 18px;
    cursor: pointer;
    font-family: inherit;
  }
  .close:hover {
    color: #1f2328;
  }
  .muted {
    color: #6b7280;
  }
</style>
