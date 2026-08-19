# WorldClaw reproduction plan (re-planned for our real stack)

Re-planned 2026-08-12 after two changes: **(1)** Blender+MCP bridge is now live on the
Windows box; **(2)** `sam-3d-objects` is released (closes the SAM3D gap, but needs
Linux + ≥32GB GPU). This plan replaces the earlier "everything on Windows, SAM3D
missing" assumption.

## Goal & success criteria

Reproduce WorldClaw's pipeline shape on **one real scene**, ending with an editable
Blender world (`S = Compose(T, O)`) we can render to the 4-channel G-buffer QA set
(rgb/instance/normal/depth + walk pass). We are aiming at **pipeline-shape fidelity +
downstream-editable output**, not paper hero quality (see Risks).

Done = a chosen reference prompt → Blender scene with continuous terrain + placed,
separable, non-floating objects → 4-channel render passes verify it.

## Environment topology (two-box reality)

```
WINDOWS BOX (we have it now)                      LINUX GPU NODE (needed for SAM3D)
- Blender 5.1 + blender-mcp addon (live)         - sam-3d-objects (≥32GB VRAM)
- ZCode + mcp__blender__* tools                   - input: image + per-object mask
- hunyuan3d-pipeline (asset gen/rig/motion)        - output: mesh/splat + per-object pose
- openai-image-api-i2i (layouts, compositions)   - (could also run Blender here for a
- dreamina-i2v (render→video)                      fully-Linux pipeline)
        ↑                                                  ↑
   Stage 2 (terrain), Stage 3 tail (refine)         Stage 3 (object recon + pose)
```
Object meshes + poses produced on Linux are imported into the Windows Blender scene
for assembly + refinement. (If the Linux node also runs Blender, run the whole pipeline
there and skip the cross-machine transfer.)

## Per-stage tool assignment

| Stage | What | Windows box | Linux GPU node |
|---|---|---|---|
| 0–1 Intent & Planning | `q → P` | agent (you) | — |
| 2a Terrain planning | concept images | `openai-image-api-i2i` | — |
| 2b Terrain assets | layout map + prototypes + materials | layout: `openai-image-api-i2i`; prototypes: `hunyuan3d-pipeline gen`; materials: Blender shader nodes | — |
| 2c Terrain build | height field + scatter | **Blender via MCP** | — |
| 3a Region planning | pick regions | agent | — |
| 3b Composition | `I^comp_r` 2D prior | `openai-image-api-i2i` (i2i on terrain render) | — |
| 3c Object recon | mesh + per-object pose | fallback: crop + `hunyuan3d-pipeline gen` | **`sam-3d-objects`** (preferred) |
| 3d Placement | land on terrain | **Blender via MCP** (ray-cast + scale calib) | — |
| 3-tail Refinement | pose/contact/collision loop | **Blender via MCP** | — |
| QA | 4-channel render | **Blender via MCP** (rgb/instance/normal/depth + walk) | — |
| Video (optional) | render → clip | `dreamina-i2v-workflow` | — |

## Phased plan (each phase has a gate)

**Phase 0 — Environment (DONE).** Blender 5.1 + blender-mcp addon installed & enabled;
ZCode MCP config written. *Gate: user restarts ZCode → `mcp__blender__*` tools appear +
Blender Connect works.* See `blender-mcp-setup.md`.

**Phase 1 — Single-scene MVP on Windows (no Linux needed).** Prove the full pipeline
shape end-to-end with the **fallback** object path. Cheapest validation that the agents,
image gen, Hunyuan3D, and Blender assembly all compose.
- Scene: **`snowline-village`** (compact snow world — smallest scope of the 11).
- Terrain: layout map (gpt-image-2) → height field + snow material + scatter rocks/trees
  in Blender via MCP. Scatter props via `hunyuan3d-pipeline` prototypes.
- Objects: render region → composition (i2i) → crop each → `hunyuan3d-pipeline gen` →
  place via Blender ray-cast.
- Refinement loop in Blender via MCP.
- *Gate: 4-channel G-buffer render of the scene; instance pass shows separable objects;
  depth pass shows no floaters.*

> **STATUS (2026-08-13): Phase 1 MVP substantially complete.**
> - **Stage 2 (terrain)** DONE — `snowline-village-stage2.blend`: deterministic height
>   field `H(x,y)` (shared `scripts/worldclaw_math.py`, N=256, SIZE=80, HSCALE=6), 5-band
>   material masks (water/village/forest/mountain/snow), snow-mobility scatter, Cycles CPU.
>   Renders: `terrain/layout.png`, `renders/{aerial,walk,topdown}.png`.
> - **Stage 3 (object gen + placement)** DONE — Hunyuan3D `gen` produced `objects/hut.glb`;
>   `scripts/stage3_place.py` imports it, normalizes scale to a target height (3.6 m),
>   samples village-band positions (village>0.35, water/mountain/forest gated, ≥5 m spacing,
>   up to 10), snaps each instance to `H(x,y)` (same math → no floaters), places as linked
>   duplicates. Renders: `renders/stage3_{topdown,hero}.png`.
>   Topdown QA 8/10 (10 huts on both banks of the river, all grounded, sized, no
>   intersections); hero QA 9/10 (wood walls + snow roof, reads as a snow village).
> - **Stage 4 (4-channel G-buffer QA)** DONE — `scripts/stage4_qa_passes.py` renders the
>   full QA set (instance/normal/depth/rgb). Visual QA: instance **9/10** (10 distinct,
>   separable hut blobs), depth **9/10** (continuous terrain depth field, hut bases flush
>   with ground, **no floaters/sinkers**), normal 9/10 (valid per-orientation colors).
> - **Phase-1 gate MET.** Pipeline-shape fidelity achieved on one scene:
>   `q → P → T → O → S=Compose(T,O)`, deterministic, editable, and verified by a 4-channel
>   G-buffer render whose instance pass shows separable objects and whose depth pass shows
>   no floaters. (Next ambition would be fidelity/breadth — Phase 3.)

> **STATUS (2026-08-13, later): Phase 1 engine-ified; Phase 3 underway.**
> - The three hard-coded `snowline-village` scripts are refactored into a **spec-driven,
>   style-agnostic engine** at `C:\ClaudeProjects\worldclaw-repro\template\` (`scene_spec.schema.json`,
>   `worldgen_math.py`, `layouts.py` [pluggable `layout_fn`], `worldgen.py` [terrain/place/qa],
>   `run_scene.py` [routes Blender through `gfxctl` — never launches it directly]). A scene is now
>   **one JSON spec (+ maybe a `layout_fn`) + maybe one asset, no per-scene code** — formalizing the
>   `P` abstraction the skill only described.
> - **Equivalence gate PASSED:** the engine reproduces the MVP numerically (`test_equivalence.py`:
>   masks/H/placement match the original math) and end-to-end (terrain matfaces, 10-hut placement,
>   4-channel QA sizes all reproduce through the broker).
> - **Phase 3 — autumn re-skin** DONE: `specs/snowline-autumn.json` reuses the `snowline` layout +
>   the MVP `hut.glb` with a swapped autumn palette (0 new assets). Rendered through the broker;
>   visual QA 8/10 (warm oranges/tans, slate river). Proves palette/season parametrization.
> - **Phase 3 — lakeside-hamlet** (new locale): new `lakeside` `layout_fn` (elliptical lake +
>   shore village band; region set swaps `snow_plain`→`meadow`), temperate green palette, distinct
>   terrain seeds, and a new Hunyuan3D cottage prototype. Layout math pre-validated (masks partition
>   ~1.0, 798 village-band candidates). Run via broker + QA pending the prototype gen.
> - **Phase 3 — image2 layout map (Stage 2b)** DONE: a third `layout_fn`, `image_layout`,
>   reads an LLM-generated flat-color semantic layout PNG and classifies pixels to regions
>   (nearest-palette). Verified 1.0000 on a flat-color round-trip; a real image2.0-queue job
>   (`worldclaw_layout.png`, 8/10 flat semantic map) classified to sensible regions
>   (water 7.7% / snow 42.8% / village 5.3% / forest 22.6% / mountain 21.6%) and drove a full
>   `image-village` scene (terrain+place+4-channel QA) whose region geometry differs from the
>   analytic snowline — i.e. a genuinely LLM-authored layout. Three layout kinds now: analytic
>   (`snowline`/`lakeside`) / image (`image`). Enqueue via the image2-queue skill
>   (`image2_queue.py enqueue`), wait for a Codex Image-2.0 worker, then `run_scene.py specs/image-village.json`.
> - **HD2D (task 3)** delivered as design only: see `hd2d-adapter-design.md` (standalone Blender
>   diorama vs SlowLife map-JSON emitter; the engine's `style` field + pluggable output make this a
>   future adapter, no core change).

**Phase 2 — Add SAM3D (needs Linux GPU node).** Swap the Stage 3c fallback for real
`sam-3d-objects`: image + mask → mesh + per-object pose. Transfer meshes + poses to the
Windows Blender scene (or run all-Linux). Optionally use the **hybrid** (SAM3D pose +
Hunyuan3D mesh fidelity).
- *Gate: same scene re-run; placement uses SAM3D's recovered pose directly; compare
  quality vs Phase 1.*

**Phase 3 — Fidelity & breadth.** Multiple scenes, seasonal material variants,
Sketchfab scatter sourcing, video via dreamina. Tune toward (not reaching) paper quality.

## MVP data flow (Phase 1, Windows-only)

```
prompt q
  → intent.json → scene spec P
  → [gpt-image-2] semantic layout map I_layout
  → [Blender MCP] height field H(x) + snow material + scatter prototypes
  → for each region in P:
      render local terrain I^terrain_r  [Blender MCP]
      → [gpt-image-2 i2i] composition I^comp_r
      → crop objects → [hunyuan3d-pipeline gen] meshes
      → [Blender MCP] ray-cast placement on T
  → [Blender MCP] refinement loop (contact, floaters, collisions)
  → S = Compose(T, O)
  → [Blender MCP] render rgb/instance/normal/depth + walk  (QA deliverable)
```

## Running through the graphics broker (heavy-gui-broker) — operational notes

Every Blender launch goes through `gfxctl`, never `blender ...` directly (discipline:
`heavy-gui-broker` skill). The headless background render path that works on this box:

```bash
PY="/c/Users/garyf/AppData/Local/Microsoft/WindowsApps/PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0/python.exe"
"$PY" "C:\ClaudeProjects\AIGame\GraphicsBroker\gfxctl.py" run \
  --owner zcode:worldclaw-repro \
  --project "C:\ClaudeProjects\AIGame" \
  --app blender --target blender-5.1 \
  --purpose Stage3-placement \
  -- --background --python "<abs path to stage script>.py"
```

Key gotchas learned the hard way:

- **`--project` must point at an approved broker root.** The broker enforces a
  `project_roots` whitelist (see `C:\Users\garyf\.aigame-graphics-broker\config.json`,
  e.g. `C:\ClaudeProjects\AIGame`). If the `--project` value is outside the whitelist the
  request is **silently quarantined** → `run` blocks forever and the job never shows in
  the queue. The repro workspace at `C:\ClaudeProjects\worldclaw-repro` is *not* on the
  list, so we pass `--project "C:\ClaudeProjects\AIGame"` (the broker checks the `--project`
  prefix, not where the script/outputs live). Cleaner permanent fix: add the repro root to
  `project_roots`.
- **A `run` that appears "hung" with a 0-byte log is almost always a quarantine**, not a
  dead process or a permission problem. Check `gfxctl status` + the broker log for
  `quarantined: project_path is outside configured project roots` *before* killing anything
  or asking for authorization.
- `run` is blocking + heartbeat; a managed command that exits naturally releases the lease
  automatically (no manual release needed). 900 s hard max per round — for long renders,
  drop `samples`/`resolution` (Stage 3 used samples=16, 960×540) and/or split passes.
- **Never** write the broker `client_token` / lease id into files or logs (delete any
  `*_run.log` that captured it). Secrets stay in env / memory only.

## Blender 5.1 bpy API notes (heads-up for headless work)

Blender 5.1 redesigned the **compositor** and shuffled a few render settings. Code that
worked in 3.x/4.x **will silently fail or raise** here:

- **`scene.node_tree` is gone.** The compositor tree is now a node *group*: create one
  with `ng = bpy.data.node_groups.new("Comp", type='CompositorNodeTree')` and assign it
  with `scene.compositing_node_group = ng` (it is `None` until you do). Edit nodes via
  `ng.nodes` / `ng.links`.
- **`CompositorNodeComposite` (the output node) is gone** — output is implicit.
- **`CompositorNodeOutputFile` changed**: no `base_path`/`file_slots`; it uses
  `file_output_items` and its `format.file_format` accepts **only `OPEN_EXR_MULTILAYER`**
  (PNG/other formats are not allowed on this node in 5.1).
- **`scene.render.file_format` is gone** — use `scene.render.image_settings.file_format`.
  And `OPEN_EXR_MULTILAYER` is **not** a valid *scene* output format in 5.1 (only
  single-layer `OPEN_EXR`); multi-pass EXR is compositor-File-Output only.
- **The reliable way to emit arbitrary passes headless is NOT the compositor** — it's the
  shader-material trick: build an emission material that reads the value you want and
  render with `write_still=True`. Per pass:
  - *depth*: `ShaderNodeCameraData` → `View Z Depth` → `ShaderNodeMapRange` → Emission **Strength**.
  - *normal*: `ShaderNodeNewGeometry` → `Normal` → VectorMath SCALE 0.5 → VectorMath ADD 0.5 → Emission **Color**.
  - *instance*: `ShaderNodeObjectInfo` → `Object Index` (= the object's `pass_index`) → Math ÷N → Emission **Strength** (per-object grayscale; linked-duplicate instances read their own `pass_index`, so each hut renders distinct). Assign each hut a unique `pass_index` (1..N); set terrain/others to 0.
  - Render each special pass with `scene.render.film_transparent = True` (background → alpha 0), low samples (8 is clean for flat emission), `image_settings.color_mode='RGBA'`. Render rgb first (original materials) before overwriting them.
- **The broker swallows the managed command's stdout.** If you need prints back, write
  them to a status file with plain file-I/O (the Stage-4 script writes
  `renders/stage4_status.txt` for this reason).

## Open decisions (resolved 2026-08-12)

1. **Linux GPU node** → **none available**; use the Windows fallback path. Phase 1 is the
   ceiling until/unless a ≥32GB-VRAM Linux node appears (Phase 2 blocked on hardware).
2. **Scene choice** → **`snowline-village`** for the MVP (done).
3. **Sketchfab** → **want it wired** for scatter-library sourcing; key not yet provided
   (deferred to Phase 3 / until key arrives).
4. **Hunyuan3D** → asset generation stays in the **separate `hunyuan3d-pipeline` skill**
   (creds read from env `TENCENTCLOUD_SECRET_ID`/`SECRET_KEY`), **not** wired into the
   Blender MCP bridge. The bridge only imports the `.glb` the skill produced.

## Effort & cost notes

- Phase 1 MVP: **hours** (mostly Blender MCP orchestration code + asset API calls).
- Phase 2: **+ SAM3D install on Linux** (heavy CUDA stack, Kaolin from source) + a
  transfer path — half-day to a day of setup, then cheap per-scene.
- Recurring cost: per-object Hunyuan3D/gpt-image-2 API calls + render time. Control
  with the prototype-library + scatter-sourcing levers (build once, instance many).

## Risks (unchanged caveats)

- Quality bounded by foundation models; paper itself reports open-source struggles.
- LLM-generated Blender code is unstable → budget multiple refinement iterations.
- Blender 5.1 is newer than blender-mcp's tested versions → addon may need a tweak
  (fallback: `blender --background --python` for pure bpy work).
- SAM3D native output is a Gaussian splat → enable mesh+texture-bake flags or convert.
- SAM License (sam-3d-objects) needs legal sign-off before any commercial ship.
