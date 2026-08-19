# WorldClaw — Reference Prompts, Asset Sourcing & QA Pattern

Operational knowledge mined from the project-page source (`web` branch of
`Tencent-Hunyuan/Hunyuan3D-WorldClaw`). These are concrete, reusable assets for our
projects: a prompt library, an asset-sourcing matrix, and a verification pattern.

## The official 3-stage model (use these labels)

```
q  ──Stage 01──▶  P  ──Stage 02──▶  T  ──Stage 03──▶  O  ──▶  S = Compose(T, O)
  Intent Analysis    Global Terrain    Regional Object         Explicit,
  & Planning         Generation        Generation & Placement  explorable,
                                                          (refinement is a       editable
                                                           closed loop INSIDE
                                                           Stage 3, not a 4th)
```
Tagline: **"Coarse to fine, global to regional."** Agents communicate through
**shared structured intermediate representations** so local edits preserve global
organization.

- **Stage 01 — Intent Analysis & Planning** (`q → P`): intent agent extracts/
  normalizes *explicit* constraints only; planning agent resolves descriptions and
  completes attributes per schema. `P` = shared semantic interface for regions,
  terrain constraints, object constraints.
- **Stage 02 — Global Terrain Generation** (`P → T`): turns constraints into
  executable specs; asset generation produces layout maps + prototypes + materials
  composed into landforms; a **render-and-inspect loop** corrects transitions and
  scattering. The same soft region weights blend **both** height and surface
  materials.
- **Stage 03 — Regional Object Generation & Placement** (`(P,T) → O`): regional
  planning agent selects supportive regions → composition images → reconstructed
  textured meshes with terrain-aligned placement → **refinement agent** checks pose,
  mesh quality, contact (closed loop per instance). Output keeps assets editable and
  reusable.

## Reference prompt library (11 real WorldClaw scenes)

Steal these as templates — they're the exact style of open-ended brief the pipeline
is tuned for. (ID = the scene's URL slug; useful for naming our output dirs.)

| Scene ID            | Theme                    | Prompt essence (abridged)                                                |
|---------------------|--------------------------|--------------------------------------------------------------------------|
| `frontier-mosaic`   | Multi-biome village      | Medieval village with snow mountains, plains, water bodies…              |
| `snowline-village`  | Compact snow world       | Snow village along both sides of a frozen river.                         |
| `painted-dunes`     | Desert landforms         | Desert adventure camp surrounded by massive coiling dragons.             |
| `island-settlement` | Compact island           | Tropical pirate-island stronghold, One-Piece adventurous atmosphere.     |
| `grand-canyon`      | Large canyon world       | Canyon with a river through it; tribal villages scattered along banks.   |
| `azure-archipelago` | Large island world       | Island with multiple Japanese-style towns, surrounded by ocean.          |
| `ember-caldera`     | Large volcanic world     | Volcanic landscape of glowing lava; volcano like a demon's lair.         |
| `desert-frontier`   | Large desert world       | PUBG-inspired desert battlefield for large-scale PvP.                    |
| `frontier-mine`     | Industrial terrain       | Gemstone mining site with excavation equipment and construction areas.   |
| `verdant-valley`    | Large mountain valley    | Realistic valley with Hobbit-style villages beneath the hills.           |
| `snowbound-outpost` | Large snow world         | Red-Alert-inspired snow valley with varied military outposts.            |

Prompt pattern that works: **[biome/terrain] + [thematic anchor / IP reference] +
[settlement/object distribution] + [scale cue (compact/large)]**.

## Asset-sourcing matrix (the seasonal insight)

WorldClaw's Spring/Summer/Autumn/Winter variants of the *same* world reveal that
**scatter assets don't have to be generated** — you mix sources per asset class:

| Season | Scatter-asset source                | Other objects source        |
|--------|-------------------------------------|-----------------------------|
| Spring | **3D coding** (procedural)          | 3D generative models        |
| Summer | **Sketchfab** (external library)    | 3D generative models        |
| Autumn | 3D generative models                | 3D generative models        |
| Winter | 3D generative models                | 3D generative models        |

**Implication for our pipeline:** classify each required asset as
- **Hero / unique object** → generate (`hunyuan3d-pipeline`).
- **Scatter / mid-scale repeat prop** (rocks, vegetation, clutter) → **prefer
  procedural (Blender) or an external library (Sketchfab) first**; only generate if
  nothing fits. This is the primary cost lever and exactly what WorldClaw does.

Add an `asset_source` field per prototype in `O_asset`: `generative | procedural |
library`.

## G-buffer QA pattern (how to prove the world is real & editable)

Every WorldClaw result scene ships **four synchronized channels from one camera
orbit**, plus a ground-level **walk** track. Render the same four passes as your QA
deliverable — they expose problems RGB hides:

| Channel   | Meaning                  | What it verifies                              |
|-----------|--------------------------|-----------------------------------------------|
| **rgb**   | Shaded appearance        | Visual quality, lighting, materials           |
| **instance** | Editable asset masks  | **Instance-level separability** (can I pick/edit each object?) |
| **normal** | Surface orientation     | Geometry quality, smoothing, contact seams    |
| **depth** | Metric scene distance   | Correct scale, no floating objects, real 3D   |

Two camera tracks per scene:
- **Orbit** — aerial pass over the finished world (global read).
- **Walk** — ground-level camera inside the scene (immersion / scale check).

**Rule:** the four channels must share one camera path + duration so they stay in
sync while looping. If `instance` bleeds across objects or `depth` shows floaters,
send those instances back to the Stage-3 refinement loop.

## Render/deliverable specs (from the page's media pipeline)

- **Result clips:** 960×540, 30 fps, H.264 High / yuv420p, ~per-scene ~3–5 MB.
  Masters ~11 Mbps; web CRF 28 (rgb) / 30 (instance, normal, depth).
- **Semantic layout maps** (`I_layout`): 1200×878 `.webp`, **alpha cut-outs**
  normalized onto one canvas so worlds are frame-comparable; no baked background.
- **Layout thumbnails:** 300×220 `.webp`.
- **Poster frames:** each clip's first frame as `.webp` (no visual jump on play).
- **Teaser:** 1080p reel, no audio, `faststart` for streaming.
- Loading: lazy — only posters load until the grid scrolls into view; switch scenes
  unmounts old clips and fetches only the new four.

Use these numbers as defaults when our projects need to render generated worlds for
review or web showcase.
