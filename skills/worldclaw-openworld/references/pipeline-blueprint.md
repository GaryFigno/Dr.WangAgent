# WorldClaw Pipeline Blueprint (executable with our tools)

This is the actionable, stage-by-stage recipe. Each stage lists: **what WorldClaw
does**, then **how we execute it with our existing skills/tools**. This is how you
turn this methodology skill into a real run for any project.

> Conventions: `q` = user text prompt. `P` = scene spec. `T` = global terrain.
> `O` = object set. `S = T ∪ O` = final world. Engine target = Blender (via MCP).

---

## Stage 0 — Intent Analysis

**Goal:** extract and normalize *only* the user's explicit constraints. Do not invent.
If the prompt is "a foggy fishing village on a cliff at dawn," capture: biome =
coastal cliff, mood/time = dawn fog, key objects = boats/huts/nets, scale cues,
forbidden additions = none stated.

**Our execution:** the coding agent (you) writes this down as a structured
`intent.json` (regions[], terrain_categories[], required_objects[], materials[],
mood, scale, hard_constraints). No tool call — pure LLM normalization.

**Exit gate:** every field is traceable to a user utterance. Flag inferred items
separately so Stage 1 can complete or reject them.

---

## Stage 1 — Scene Planning

**Goal:** complete missing information and resolve ambiguities → `P`.

**Our execution:**
1. For each region, decide: terrain category, rough footprint, object requirements,
   material palette, adjacency relations.
2. If a fact is ambiguous, pick a sensible default and record it as an *assumption*
   (WorldClaw's Scene Planning Agent does exactly this).
3. Optional but recommended: invoke a **search tool** (web search) for reference
   imagery of the biome/architecture to anchor later image generation.

**Exit gate:** `P` fully specifies regions + per-region object lists + terrain
categories. Nothing required by Stage 2 is left TBD.

---

## Stage 2 — Global Terrain Generation (build T)

### 2a. Terrain Planning
**WorldClaw:** Terrain Planning Agent converts `P` into an executable terrain spec;
may call a search tool for references and generate scene **concept images**.

**Our execution:**
- Use `openai-image-api-i2i` (gpt-image-2) to generate 1–3 concept images of the
  whole scene from `P` (top-down + hero angle). These guide layout color choices.

### 2b. Terrain Asset Generation
**WorldClaw:** produces (i) a **semantic layout map** `I_layout` (distinct colors per
terrain category), (ii) **reusable 3D asset prototypes** `O_asset`, (iii) surface
materials.

**Our execution:**
- **Layout map:** prompt gpt-image-2 for a top-down semantic map with a fixed
  color→category legend (e.g. `#3a7d44` forest, `#c2b280` sand, `#4a90d9` water,
  `#7a7a7a` rock). Keep the legend fixed so Stage 2c can parse it.
- **Asset prototypes:** for each distinct prop in `P` (rock, pine, hut, boat…),
  generate one reference image, then run `hunyuan3d-pipeline` `gen` to get a clean
  mesh + PBR. Store as `O_asset` — a **library**, no instance transforms yet.
- **Materials:** prefer **procedural** Blender shader-node materials (tileable,
  parameter-adjustable) for ground/rock/water; reserve generative textures for
  hero-detail patches. See `method-details.md`.

### 2c. Terrain Generation (the actual build)
**WorldClaw:** parse semantic map → composite region-aware height field → boundary
smoothing → assign materials → scatter mid-scale assets, snapping to normals.

**Our execution (Blender via MCP):**
1. Read `I_layout` into a low-res region mask per category.
2. Build the height field per the formula in `method-details.md`:
   `H(x) = Σ_r m̃_r(x) · [h_r + Σ_k N_{r,k}(x) + Σ_j G_{r,j}(x)]`
   — base elevation `h_r` + noise bands `N` + geomorphic operators `G` (peaks,
   ridges, dunes), blended by normalized soft masks `m̃_r` for smooth boundaries.
3. Apply **boundary smoothing** across region seams.
4. Assign materials by category; layer procedural + generative where needed.
5. **Scatter** mid-scale prototypes from `O_asset`: sample candidate locations by
   per-region density/affinity, then scale + orient each instance to the local
   surface normal.

**Exit gate:** `T` exists as one continuous editable mesh with materials + scattered
props. Render a sanity still before proceeding.

---

## Stage 3 — Regional Object Generation & Placement (build O)

### 3a. Regional Planning
**WorldClaw:** Regional Planning Agent prioritizes regions with
**uninstantiated object requirements**.

**Our execution:** iterate `P`'s object lists; for each region still missing required
objects, queue it for 3b–3d.

### 3b. Terrain-conditioned composition
**WorldClaw:** render local terrain to `I^terrain_r`; an image-editing model produces
a region composition `I^comp_r` as a **2D layout prior**.

**Our execution:**
1. Render the region's local terrain to an image (Blender via MCP).
2. Use `openai-image-api-i2i` **image-to-image** with the terrain render as the
   reference, asking it to compose the required objects into the scene. Keep terrain
   identity locked (reference-preserving).

### 3c. Editable mesh reconstruction
**WorldClaw:** SAM3 segments 2D instances from `I^comp_r`; SAM3D reconstructs mesh
`M_i` + appearance `U_i` + local-to-object-camera transform + intrinsics; Hunyuan3D
then upgrades geometry + PBR.

**Our execution:**
- **Preferred (Linux + ≥32GB GPU):** run `facebookresearch/sam-3d-objects` —
  `inference(image, mask)` returns geometry + texture + per-object
  `rotation/translation/scale` (the exact placement inputs) in one call. Enable
  `with_mesh_postprocess` + `with_texture_baking` for a textured mesh. For max
  fidelity use the **hybrid**: take SAM3D's pose, re-generate the mesh with
  `hunyuan3d-pipeline` `gen` from the crop. See `sam3d-integration.md`.
- **Windows fallback (no Linux GPU yet):**
  1. Segment each object from `I^comp_r` (SAM/SAM2/SAM3, or manual crop).
  2. Run `hunyuan3d-pipeline` `gen` on each crop → mesh + PBR (collapses seg+recon;
     you lose SAM3D's per-object pose — recover it via Stage 3d ray-casting).
  3. Optionally `rig`/`motion` for characters via the same skill.

### 3d. Placement recovery
**WorldClaw:** cast a ray through the object-center pixel in the object-camera;
intersect the camera-space mesh → recovers depth/pose. Cast a second ray from the
**terrain camera** through the same pixel → terrain anchor. Then image-space scale
calibration + joint depth/scale search along the terrain-camera ray.

**Our execution (Blender via MCP):**
1. For each object, you know: its center pixel in `I^comp_r`, the object-camera
   intrinsics, and the region's terrain-camera intrinsics.
2. Ray-cast from object camera through center pixel onto the object mesh → object
   depth.
3. Ray-cast from terrain camera through the same pixel onto `T` → anchor point +
   surface normal.
4. Calibrate scale by matching the object's projected size in `I^comp_r`, then run a
   joint (depth, scale) search on the terrain-camera ray to minimize projection
   error. Place instance at the solved transform.
5. Reuse from `O_asset` when the same prototype appears multiple times.

**Exit gate:** every required object in `P` is instantiated on `T` with a recovered
transform; nothing floats or clips egregiously (Stage 4 cleans the rest).

---

## Stage 4 — Scene Refinement (render-based agent)

**WorldClaw:** Scene Refinement Agent connects to the 3D engine (Blender via
BlenderMCP), maintains a **task queue**, and refines *objects* (pose, mesh quality)
and *terrain* (collisions, support surfaces, floating objects).

**Our execution (Blender via MCP loop):**
1. Open the scene; enable the MCP server.
2. Drive a refinement queue: for each suspect instance — check pose plausibility,
   mesh integrity, ground contact, inter-object collision, terrain support.
3. Apply fixes (re-roll a bad mesh via `hunyuan3d-pipeline`, nudge transforms,
   re-scatter). Budget several iterations — the paper notes LLM-generated DCC code
   is unstable and needs repeated refinement.
4. Render final stills/turntables; optionally feed hero stills to
   `dreamina-i2v-workflow` for cinematic video.

**Exit gate:** `S = T ∪ O` reviewed in viewport; no floating/intersecting assets on
the hero paths.

---

## Cheat-sheet: stage → skill

```
Stage 0  Intent        → (agent only)
Stage 1  Scene plan    → web search for refs (optional)
Stage 2a Terrain plan  → openai-image-api-i2i (concept images)
Stage 2b Terrain assets→ openai-image-api-i2i (layout map) + hunyuan3d-pipeline gen (prototypes)
Stage 2c Terrain build → Blender via MCP (height field + materials + scatter)
Stage 3a Region plan   → (agent only)
Stage 3b Composition   → openai-image-api-i2i i2i (terrain render → composition)
Stage 3c Mesh recon    → hunyuan3d-pipeline gen (+ rig/motion)
Stage 3d Placement     → Blender via MCP (ray-cast + scale calibration)
Stage 4  Refinement    → Blender via MCP loop (+ re-roll via hunyuan3d-pipeline)
After    Video          → dreamina-i2v-workflow (optional)
```
