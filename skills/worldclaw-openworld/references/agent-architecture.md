# WorldClaw Agent Architecture

WorldClaw is an **agent loop**: one orchestrating LLM (the paper uses Claude
Opus 4.8) dispatches a fixed cast of **seven specialized sub-agents**. Each sub-agent
has a narrow job, defined inputs/outputs, and a set of tools (foundation models or
the DCC engine). When we emulate WorldClaw, the coding agent *plays* each role in
sequence — read this to know which hat you're wearing at each step.

## The cast

| # | Agent                          | Role (one line)                                                | Inputs → Outputs                                                          | Tools it may call                            |
|---|--------------------------------|----------------------------------------------------------------|---------------------------------------------------------------------------|----------------------------------------------|
| 1 | Intent Analysis                | Normalize *only* explicit user constraints; no invention.      | `q` → normalized constraints                                              | none (pure LLM)                              |
| 2 | Scene Planning                 | Complete missing facts, resolve ambiguities → scene spec `P`.  | constraints → `P` (regions, terrain, objects, materials, relations)        | none (LLM); may consult intent               |
| 3 | Terrain Planning               | Turn `P` into an executable terrain spec; gather references.   | `P` → terrain spec + concept images                                       | **search tool**, image gen (concept)         |
| 4 | Terrain Asset Generation       | Produce layout map, reusable asset prototypes, materials.      | terrain spec + concepts → `I_layout`, `O_asset`, materials                | image gen (layout), 3D gen (prototypes)      |
| 5 | Terrain Generation             | Parse map → height field → materials → scatter.                | `I_layout`, `O_asset` → `T`                                               | DCC (Blender) — geometry, shaders, scatter   |
| 6 | Regional Planning              | Pick regions with uninstantiated object needs.                 | `P`, `T` → region work queue                                              | none (LLM scheduler)                         |
| 7 | Scene Refinement (render-based)| Refine objects + terrain via the DCC engine; task queue.       | `S = T ∪ O` → refined `S`                                                 | DCC (Blender via MCP), 3D gen (re-roll)      |

> Note: the **object reconstruction** path (composition → SAM3 → SAM3D → Hunyuan3D →
> placement recovery) is run *between* agents 6 and 7 as the mechanism that actually
> builds `O` for each prioritized region. See `pipeline-blueprint.md` Stage 3.

## How to emulate each agent with our stack

**1. Intent Analysis** — the coding agent writes a strict `intent.json`. Discipline:
every field must quote the user; inferred items go in a separate `assumptions` list.
This role exists specifically to *prevent* the planner from hallucinating features.

**2. Scene Planning** — the coding agent expands `intent.json` into the full scene
spec `P`. When facts are missing, choose defaults and mark them assumptions (mirrors
WorldClaw's "complete the missing information, resolve ambiguities").

**3. Terrain Planning** — call web search for biome/architecture reference imagery;
call `openai-image-api-i2i` for 1–3 concept images. Output a terrain spec
(category per region, geomorphic operators desired, density hints).

**4. Terrain Asset Generation** — two sub-calls:
- `openai-image-api-i2i`: top-down semantic **layout map** with a fixed
  color→category legend.
- `hunyuan3d-pipeline` `gen` once per distinct prop → the reusable **prototype
  library** `O_asset` (no transforms yet).

**5. Terrain Generation** — drive **Blender via MCP**: ingest the layout map,
compose the region-aware height field, smooth boundaries, assign materials, scatter
prototypes from `O_asset` snapped to normals. (Formula in `method-details.md`.)

**6. Regional Planning** — trivial scheduler: walk `P`, find regions whose object
requirements aren't yet on `T`, enqueue them.

**7. Scene Refinement (render-based)** — this is the **BlenderMCP loop**. Open a
task queue of suspect instances/contacts; iteratively fix pose, mesh quality,
collisions, support surfaces, floating objects; re-roll bad meshes through
`hunyuan3d-pipeline`. The paper stresses this loop runs *multiple rounds* — budget
for it.

## Why the agent decomposition matters for us

- **Separation of "no invention" (Intent) from "completion" (Scene Planning)** stops
  the common failure mode where a scene generator silently adds stuff the user never
  asked for. Mirror this: keep an `assumptions` list and surface it to the user.
- **Prototypes before instances** (agent 4 before 3d) is what makes the world
  *editable and reusable*: build each asset once, instance it many times. This is
  also the lever that controls cost (one of WorldClaw's stated limitations).
- **A dedicated render-based refinement agent** acknowledges that LLM-generated DCC
  code is unreliable. Don't trust the first placement; always run the refinement
  queue.
