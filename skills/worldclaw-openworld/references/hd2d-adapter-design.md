# HD2D adapter design (WorldClaw → HD-2D maps)

HD-2D (Octopath-Traveler style) = **2D pixel sprites + 3D low-poly diorama +
tilt-shift depth-of-field + bloom + fixed oblique camera + pixel/painted textures**.
This doc records how the WorldClaw pipeline maps onto HD2D and confirms the
`template/` engine is already shaped to accept an HD2D adapter. Two paths; both
reuse the deterministic **planning + terrain height-field + placement** core and
diverge only at the output/style layer.

## Why it mostly works already

Decompose HD2D against the WorldClaw stages:

| Stage | WorldClaw (openworld) | HD2D delta |
|---|---|---|
| q→P planning | agent → scene spec `P` | identical |
| T terrain | continuous height field + PBR | same engine; HD2D = **terraced** (quantize H) + flat water plane + pixel/painted textures instead of PBR |
| O objects | Hunyuan3D 3D meshes | **hybrid**: 3D structures via Hunyuan3D (low-poly + pixel-baked); **2D billboard sprites** for foliage/props/characters (pixel-art PNG on a camera-facing quad) |
| S placement | snap to H(x,y) | same math + billboard orientation (face camera) |
| render/post | perspective + scatter | oblique **perspective tilt-shift** camera + strong DoF + bloom (+ optional pixelation) |

Two clean reuses:
1. **The Stage-4 `qa_depth` pass is the tilt-shift DoF input.** HD2D's
   "blur top + bottom, sharp middle" is depth-driven DoF — the depth pass the
   engine already emits is exactly the signal a tilt-shift needs (in-camera Cycles
   DoF, or a post pass keyed off depth).
2. **Instance/placement math is unchanged**; only a new object class ("billboard")
   is added.

So HD2D ≈ "WorldClaw template with `style=hd2d`": the structural pipeline is
style-agnostic; style is a swappable skin. The `template/` engine threads a `style`
field and a pluggable `layout_fn`, so an HD2D path slots in without touching the
core.

## Path 1 — standalone Blender HD2D diorama (`style="hd2d"` in worldgen.py)

A self-contained render, no game project. Concrete deltas to implement in the engine:

- **Camera**: perspective, oblique, FOV ~18–28°, distance ~19–74 m, strong DoF
  (`cam_data.dof.focus_distance` + aperture). (Note: real SlowLife uses
  *perspective* tilt-shift, NOT orthographic.)
- **Terrain**: quantize the height field to N discrete levels for plateaus + cliff
  edges (`np.round(H * k) / k`); flat water plane at a fixed z.
- **Materials**: pixel/painted textures — source via `openai-image-api-i2i`
  (pixel-art tileables), apply as image textures (nearest filter) instead of PBR.
- **Billboard sprites**: a new placement kind — camera-facing quads carrying a
  pixel-art PNG (trees, props, characters). Add a `billboard` constraint
  (`TRACK_NEGATIVE_Z` to camera) and a `billboards[]` block to the spec.
- **Post**: bloom (a bright-pass emission layer; the 5.1 compositor is redesigned,
  so prefer Cycles DoF + a bloom material over compositor nodes) and optional
  final-frame pixelation (downscale + nearest upscale).

Effort: ~30% new code (billboard placer, terraced terrain, pixel textures, DoF),
~70% reuse. One demo scene proves it.

## Path 2 — SlowLife map-JSON emitter (the high-value target)

`C:\ClaudeProjects\AIGame\SlowLife` is a Godot-4.3 Forward+ HD-2D farming ARPG that
**already has a data-driven map system**: `game/scripts/hd2d/slice_builder.gd` reads
`game/data/maps/*.json` (existing: `farm_slice.json`, `morningstream_outskirts.json`),
and the project convention is literally *"add a map = one JSON + a batch of assets,
no code."* So the natural HD2D move is an **emitter** that turns a WorldClaw scene
spec into SlowLife's map format.

Adapter (future `slowlife_emitter.py`, separate from the Blender engine):
- input: a WorldClaw `spec` (+ its computed masks/height field / placement list)
- output:
  - `AIGame/SlowLife/game/data/maps/<name>.json` conforming to the `farm_slice.json`
    schema — `grid`, `hero`, `terrain` (heightfield params), `camera` (mode/yaw/pitch/
    dist/fov/DoF/follow), `environment` (grade/sun/fill/fog/sky/vignette), `time_of_day`
    overrides, `cottage` (3D building spec), `structures`, `warm_lights`, `path`,
    `terraces`, `tree_belt`, `beds`, `dressing` (scatter), `foreground` cards,
    `player`, `bounds`
  - pixel assets into `game/assets/textures/hd2d/<region>/` at **32 px/m** (with `_hi`
    dual-res), sourced via `openai-image-api-i2i` / `make_hd2d_pixel_assets.py`

Consumed at runtime by `slice_builder.gd` (and the planned generalized `MapBuilder`
called for in `docs/knowledge/01-HD2D地图搭建总方案.md` §4).

Caveats:
- Must match SlowLife conventions exactly (32 px/m, ToD overrides, cottage spec,
  Forward+ post-FX). Read `farm_slice.json` + `slice_builder.gd` before building.
- **Verification needs Godot 4.3**, which is NOT a current broker target (broker
  ships godot-4.6.1 / 4.6 only; SlowLife is on 4.3, siblings on 4.6). Emitting JSON
  needs no Godot; validating that it loads/renders correctly does. Add a 4.3 target
  or accept JSON-only validation until then.
- `C:\ClaudeProjects\AIGame` is already a whitelisted broker root, so HD2D asset
  work targeting SlowLife needs no broker config change.

> **STATUS (2026-08-13): Path 2 emitter implemented (experimental).**
> `C:\ClaudeProjects\worldclaw-repro\template\slowlife_emitter.py` turns a WorldClaw spec
> into a SlowLife map JSON consumed by `slice_builder.gd`. It down-fits the WorldClaw
> height field to SlowLife's parametric terrain (`north_rise`+`swell`+ a `flat_zone`
> pinned around the object cluster + an optional `pond` for compact water bodies), maps
> WorldClaw placement positions to hero-local cells, and emits `structures` billboards
> reusing SlowLife's **existing** farm art (farmhouse/house_a/apple_tree/… — no GLB).
> Three maps emitted + statically validated (all DIRECT keys present, all art resolves):
> `SlowLife/game/data/maps/worldclaw_{lakeside-hamlet,snowline-village,snowline-autumn}.json`.
>
> **STATUS (2026-08-13): Path 2 VERIFIED — renders in-engine.** Despite SlowLife's
> `project.godot` saying 4.3, the slice **does run under the bundled Godot 4.6.1** via the
> broker. Two snags, both solved:
> - The broker doesn't propagate client env, and the slice selects its map via `SLICE_MAP` /
>   triggers capture via `HD2D_OUTPUT` env. Worked around with a tiny runner,
>   `SlowLife/game/tools/worldclaw_capture.gd` (extends `SceneTree`), that sets those env
>   vars **in-process** (`OS.set_environment`) before loading the slice scene — so the slice's
>   own `HD2D_OUTPUT` capture (`hd2d_pixel.png` + quit) fires. Run via the broker:
>   ```
>   gfxctl run --app godot --target godot-4.6.1-gui --project C:\ClaudeProjects\AIGame \
>     --purpose worldclaw-hd2d-capture \
>     -- --path <SlowLife/game> --log-file <out.log> --script res://tools/worldclaw_capture.gd
>   ```
> - **CRITICAL Godot-4.6 gotcha:** a `--script` SceneTree's `_init` runs *before* autoload
>   globals (e.g. `EventBus`) are registered, so loading `hd2d_slice.tscn` (whose
>   `slice_builder.gd` references `EventBus`) in `_init` fails to compile (`Identifier not
>   found: EventBus`). Fix: **defer the scene load to the first `process_frame` tick** (by then
>   autoloads are registered). The runner does this.
> Result: the slice builds the emitted map (Ground + LakeWater + lily pads/cattails +
> `farmhouse_hi` + `apple_tree` billboards + 70+ ground-cover meshes + PostHD2D tilt-shift +
> HUD) and writes `hd2d_pixel.png`. Visual QA 8/10 (coherent lakeside HD-2D diorama). A few
> non-fatal `No loader found for …png` warnings (two missing normal/noise textures) don't
> block the render. The emitter also emits **warm window-lights** (`warm_lights`, one per
> structure) + scattered **dressing** props (barrel/hay/lantern/well) for a cozier scene
> (re-rendered lakeside ~8.5/10, `SCRIPT ERROR` count 0). Two emitter gotchas recorded:
> `dressing.placements` use **flat** `[asset, cx, cy, metres, footprint, shadow]` (NOT
> `[asset, [cx,cy], ...]` like `structures`), and emit `"paths": []` to skip the legacy
> single-run path that otherwise errors on a missing `path.x`. Path 1 (standalone Blender
> diorama) remains unbuilt.

## Recommendation

Start with **Path 1** (standalone diorama) to prove the engine's `style=hd2d` branch
cheaply and self-contained; then pursue **Path 2** (SlowLife emitter) as the
production target, gated on reading the SlowLife schema + resolving the 4.3
verification gap. Either way, no change to the deterministic core — only a new
output adapter — which is exactly the style-agnostic shape the `template/` engine
was refactored into.
