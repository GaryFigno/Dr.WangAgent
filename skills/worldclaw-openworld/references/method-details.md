# WorldClaw Method Details

Faithful technical detail from arXiv:2608.05248. Use this when you need the actual
math / specs rather than the high-level recipe (which lives in `pipeline-blueprint.md`).

## Terrain height field (Stage 2c)

Global terrain height `H(x)` is a **weighted sum over regions** of per-region
elevation models, blended smoothly so region boundaries don't tear:

```
H(x) = Σ_r  m̃_r(x) · [ h_r  +  Σ_k N_{r,k}(x)  +  Σ_j G_{r,j}(x) ]
```

- `r` indexes regions (from the semantic layout map).
- `h_r` — base elevation of region `r`.
- `N_{r,k}` — noise components (multi-octave) giving local irregularity.
- `G_{r,j}` — **geomorphic operators**: explicit landform shapes such as peaks,
  ridges, dunes, valleys.
- `m̃_r(x)` — **normalized soft weights** (masks) for region `r`; normalized so
  `Σ_r m̃_r = 1` everywhere. Soft masks give smooth blending; hard boundaries are
  then **boundary-smoothed** in a post-step.

Implementation tip: compute `m̃_r` from the color channels of the semantic layout
map (one mask per category), feather them, normalize per-pixel, then evaluate the
bracket per region and blend.

**Confirmed from the page source:** the **same soft region weights `m̃_r` also blend
surface materials** — not just height. So one normalized mask set drives both the
geomorphic composite and the material mix, which is why region boundaries stay
coherent across geometry and appearance simultaneously.

## Materials (Stage 2b/2c)

WorldClaw **balances two pathways**:
- **Generative texture synthesis** — for local, irregular, hard-to-parameterize
  detail (e.g. moss patches, weathering).
- **Procedural material generation** — via **Blender material (shader) nodes**,
  producing **tileable, parameter-adjustable** surface materials. This is the
  preferred path for ground/rock/water where you want consistency and reuse.

For us: default to **procedural Blender shader-node materials** (tileable, driven by
a few parameters), and layer generative textures only for hero-detail patches.

## Reusable asset prototypes (Stage 2b → 3d)

`O_asset` is built **without** instance-specific position/scale/orientation. Hunyuan3D
converts reference images into the prototype set; later stages (scatter in 2c,
placement recovery in 3d) instance from it. This is the key to editability and cost
control — generate once, place many.

## Regional object reconstruction (Stage 3b–3c)

1. **Render local terrain** → image `I^terrain_r`.
2. **Composition** — an image-editing model produces `I^comp_r`, a **2D layout
   prior** that places the required objects into the rendered terrain.
3. **2D instance extraction** — text-guided **SAM3** lifts individual objects out of
   `I^comp_r`.
4. **Single-view 3D reconstruction** — **SAM3D** predicts, per object `i`:
   - object mesh `M_i`
   - appearance attributes `U_i`
   - local-to-object-camera transformation
   - camera intrinsics
5. **Geometry/PBR upgrade** — **Hunyuan3D** (2.1 / 2.5) improves local geometry and
   produces production-ready PBR materials.

> **SAM 3D Objects is now released** (`facebookresearch/sam-3d-objects`, arXiv
> 2511.16624) and does steps 3–5 in one call: image + per-object mask → geometry +
> texture + per-object transform (`rotation/translation/scale` in a shared scene
> frame). Native output is a Gaussian splat; enable `with_mesh_postprocess` +
> `with_texture_baking` for a textured mesh. ⚠️ **Linux-64 + ≥32GB GPU only** — on
> Windows keep the fallback (crop + `hunyuan3d-pipeline` `gen`, then recover the
> transform via placement ray-casting). Full API + prereqs + the hybrid
> (SAM3D pose + Hunyuan mesh fidelity) in `sam3d-integration.md`.

## Placement recovery (Stage 3d)

Each object's 3D pose is recovered by **two ray casts + a joint scale/depth search**:

1. **Object ray:** cast a ray through the object's center pixel (in the object camera)
   and intersect it with the camera-space mesh → recovers object depth and pose.
2. **Terrain anchor ray:** cast a ray from the **terrain camera** through the same
   pixel and intersect with `T` → gives the anchor point + surface normal on the
   terrain.
3. **Image-space scale calibration:** match the object's projected footprint/size in
   `I^comp_r` to fix its absolute scale.
4. **Joint search:** optimize (depth, scale) **along the terrain-camera ray** to
   minimize the projection error between the placed instance and its 2D appearance in
   `I^comp_r`.

Output: a per-instance transform (translation on terrain, scale, orientation to
normal) — the instance is grounded, not floating.

## Foundation-model matrix

| Model              | Role in WorldClaw                                                | Our equivalent                                            |
|-------------------|------------------------------------------------------------------|----------------------------------------------------------|
| Claude Opus 4.8   | Agent brain — orchestrates all stages, writes DCC code            | the coding agent (you)                                   |
| GPT-Image-2       | Semantic layout map `I_layout`; region composition `I^comp_r`     | `openai-image-api-i2i` (gpt-image-2) / `image2-queue`    |
| SAM3              | Text-guided 2D instance segmentation of `I^comp_r`                | any segmenter (SAM / SAM2 / SAM3)                              |
| SAM3D             | Single-view 3D object reconstruction (mesh + transform)          | **sam-3d-objects** (Linux GPU); fallback `hunyuan3d-pipeline` `gen`                 |
| Hunyuan3D 2.1/2.5 | High-fidelity textured mesh + PBR materials                      | `hunyuan3d-pipeline` (`gen`, optionally `rig`/`motion`)  |
| Blender + MCP     | Assembly, procedural materials, refinement loop                   | Blender + MCP server (BlenderMCP)                        |

## Implementation specs (from the paper)

- **Hardware:** 4× NVIDIA H20 GPUs.
- **DCC engine:** Blender 5.1.1, driven via **BlenderMCP** (Model Context Protocol).
- **Texture resolution:** **2048×2048** PBR maps for large objects; **1024×1024**
  for small objects.
- **Agent model:** Claude Opus 4.8 as the underlying agent.
- **Render/QA deliverable (from the page source):** each scene ships 4 synchronized
  G-buffer channels from one camera orbit — **rgb** (shaded), **instance** (editable
  masks), **normal** (orientation), **depth** (metric) — at 960×540/30 fps, plus a
  ground-level **walk** pass. Render these to verify the world is real-3D and
  instance-editable. Full pattern + 11 reference prompts + the seasonal asset-sourcing
  matrix live in `scenes-prompts-and-qa.md`.

## Evaluation / baselines

Evaluation in the paper is **qualitative** (terrain organization, content richness,
prompt alignment, free-viewpoint appearance). No quantitative metric tables or
ablations were reported. Baselines compared against: **SynCity, Marble,
MajutsuCity, WorldGen, GPT 5.6 Sol**.

## Limitations (plan around these — they directly affect our runs)

1. **Bounded by foundation models.** Visual quality ceilings are set by the image/3D
   models. The authors note open-source models frequently struggled with code
   generation and preserving object appearance. → Re-roll assets; use
   `openai-image-api-i2i` reference-based edits to preserve identity.
2. **Unstable LLM-generated DCC code.** Blender-API programs and shader-node graphs
   often mis-estimate scale or break node connectivity → expect multiple refinement
   iterations; always validate in the viewport before accepting.
3. **High latency / cost.** Per-object reconstruction plus multi-round agentic
   refinement is expensive → build the prototype library `O_asset` once and instance
   aggressively; avoid per-instance reconstruction when a prototype fits.
