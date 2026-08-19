---
name: worldclaw-openworld
description: >
  Agentic coarse-to-fine blueprint for generating large-scale, explorable, editable
  3D open-world scenes from a single open-ended text prompt — derived from Tencent
  Hunyuan's WorldClaw (arXiv 2608.05248, Guo/Li/Li/Huang, 2026). Use whenever a
  project needs to generate a whole 3D world / open-world scene / large terrain with
  placed reusable assets from text ("生成一个开放世界场景", "生成大世界地形",
  "text-to-scene", "text-to-world", "procedural open world", "3D world from prompt"),
  OR when architecting a multi-agent scene-generation pipeline that composes our
  generative tools (Hunyuan3D assets, image generation for semantic layouts, Blender
  via MCP for assembly). IMPORTANT: this is a REFERENCE-ARCHITECTURE / METHODOLOGY
  skill — WorldClaw released NO code or CLI as of 2026-08 (the GitHub repo is a
  citation stub). It tells you HOW to orchestrate the tools you already have
  (hunyuan3d-pipeline, openai-image-api-i2i, dreamina-i2v-workflow, Blender MCP) into
  the published WorldClaw pipeline. Cross-reference hunyuan3d-pipeline for the
  per-asset generation primitive that WorldClaw itself uses internally.
---

# WorldClaw — Agentic 3D Open-World Generation Blueprint

## Overview

WorldClaw (Tencent Hunyuan, arXiv 2608.05248) is a **fully agentic, coarse-to-fine**
framework that turns one open-ended text prompt into a large-scale, freely explorable,
**editable** 3D open world: a single globally coherent terrain plus instance-level
3D assets that can be reused and edited downstream.

The whole system is an **LLM agent loop** (the paper uses Claude Opus 4.8 as the
agent brain) that drives a fixed cast of **specialized sub-agents**, each calling
**foundation models** (GPT-Image-2, SAM3, SAM3D, Hunyuan3D) and a **3D DCC tool
(Blender via MCP)**. The final world is:

```
S = Compose(T, O)        # official formula — "explicit, explorable, editable"
#  T = global terrain foundation (one continuous height-field + materials + scattered mid-scale props)
#  O = set of editable instance meshes, placed on T with recovered camera-accurate poses
```

This skill is the **operational blueprint**: which stage does what, in what order,
and — for our stack — which existing tool to call at each step.

## Status — read this first (do not hallucinate a CLI)

- **No runnable pipeline code.** The GitHub repo has two branches, neither ships a
  CLI/API (verified 2026-08-12): `main` = a stub (title, `pipeline.jpg`, news,
  citation, links); `web` = the project-page **source** (React/Vite) — rich
  reference content (canonical text, 11 reference prompts, G-buffer QA pattern,
  asset-sourcing matrix) but **not** executable generation code. **There is no
  `worldclaw` binary, pip package, or API.**
- Therefore: **never invent a `worldclaw` command.** This skill is implemented by
  *you orchestrating* the components below. Treat it as an architecture to assemble,
  not a product to invoke.
- Our value-add: the components WorldClaw depends on are **already in our skill
  library** (Hunyuan3D for assets, OpenAI image API for layouts/compositions,
  Blender MCP for assembly). We can build a WorldClaw-style pipeline today.

## The pipeline (coarse-to-fine, global-to-regional)

Three sequential stages, each an agent-driven sub-loop. Read
`references/pipeline-blueprint.md` for the actionable per-stage recipe with our
tool calls; `references/agent-architecture.md` for each agent's exact role;
`references/method-details.md` for the terrain height-field math, placement-recovery
ray-casting, and implementation specs.

**Stage 0–1 — Intent Analysis & Scene Planning**
Intent Analysis Agent normalizes *only* the user's explicit constraints (no
invention). Scene Planning Agent completes the missing facts and resolves
ambiguities → a **scene specification P** (regions, terrain categories, object
requirements, materials, spatial relations).

**Stage 2 — Global Terrain Generation (build T)**
Terrain Planning Agent (may invoke a **search tool** for references + emit concept
images) → Terrain Asset Generation Agent (semantic layout map, reusable 3D asset
prototypes, surface materials) → Terrain Generation Agent (parse semantic map →
composite a region-aware **height field** → assign materials → scatter mid-scale
assets, snapping scale/orientation to surface normals).

**Stage 3 — Regional Object Generation & Placement (build O)**
Regional Planning Agent picks regions with still-uninstantiated object needs →
render the local terrain to an image → an image-editing model composes a **2D layout
prior** → SAM3 extracts 2D instances → **SAM3D** reconstructs a mesh + per-object
camera transform → **Hunyuan3D** upgrades geometry + PBR → **placement recovery**
via ray-casting + image-space scale calibration lands each asset on the terrain.

**Stage 3 (tail) — Scene Refinement (render-based agent)**
Per the paper this is a **closed loop inside Stage 3**, not a separate phase: the
refinement agent connects to the 3D engine (**Blender via MCP / BlenderMCP**),
maintains a **task queue**, and iteratively refines *objects* (pose, mesh quality)
and *terrain* (collisions, support surfaces, floating objects), re-rendering after
each edit. Then `S = Compose(T, O)`.

## Our tool mapping (how we execute each stage)

| WorldClaw component           | Role in pipeline                          | Our skill / tool to use                                                          |
|------------------------------|-------------------------------------------|----------------------------------------------------------------------------------|
| Agent brain (Opus 4.8)       | Orchestrates all stages, writes DCC code   | The coding agent itself (you) — no external call                                 |
| GPT-Image-2 (layout + comp)  | Semantic layout map; regional composition  | `openai-image-api-i2i` (gpt-image-2) / `image2-queue`                            |
| SAM3 / SAM3D                 | 2D instance seg + single-view 3D recon     | **Released: `facebookresearch/sam-3d-objects`** (image+mask → splat/mesh + per-object pose). ⚠️ Linux-64 + ≥32GB GPU only; on Windows keep the fallback (hand-segment + `hunyuan3d-pipeline` `gen`). See `references/sam3d-integration.md`          |
| Hunyuan3D (2.1/2.5)          | High-fidelity mesh + PBR; asset prototypes | `hunyuan3d-pipeline` (`gen` → `rig` → `motion`)                                  |
| Blender via MCP (BlenderMCP) | Assembly, materials, refinement            | **✅ live** — Blender 5.1 + blender-mcp addon installed & enabled; ZCode `mcp.servers.blender` registered. Procedural materials via shader nodes. Launch + creds: `references/blender-mcp-setup.md`             |
| Mid-scale scatter (rocks etc)| Terrain Generation sub-step                | **Prefer procedural (Blender) or an external lib (Sketchfab) first**; generate via `hunyuan3d-pipeline` only if no fit — WorldClaw's Spring/Summer variants source scatter props exactly this way |
| QA / verification (4-channel)| Prove real-3D + editability of the world   | Render synchronized **rgb + instance + normal + depth** from one camera orbit (+ a ground-level walk pass). `instance` = editable masks, `depth` = no floaters. See `references/scenes-prompts-and-qa.md` |

When a project needs **video/animation** of the generated world afterwards, chain
into `dreamina-i2v-workflow` (image→video) on rendered stills.

## When to use this skill vs. the per-asset skills

- **Single asset / character / prop** → use `hunyuan3d-pipeline` or `tripo3d-pipeline`
  directly. Don't pull in the WorldClaw orchestration for one mesh.
- **A whole placeable scene / level / open world from a prompt** → use this skill.
  It still calls `hunyuan3d-pipeline` under the hood for each asset.
- **Open-world *terrain only*** (no objects) → run Stages 0–2 of the blueprint.

## Known limitations (plan around these)

- **Quality is bounded by the foundation models.** Open-source image/3D models in the
  paper struggled with code generation and preserving object appearance — expect to
  re-roll assets and guard appearance consistency (use `openai-image-api-i2i`
  reference-based edits).
- **LLM-generated DCC code is unstable.** Blender-API programs and shader-node graphs
  frequently mis-estimate scale or miss node connections → budget multiple
  refinement iterations in Stage 4; validate in the viewport.
- **High latency / cost.** Per-object reconstruction + multi-round agentic refinement
  is expensive. Generate reusable asset *prototypes* once, then instance them.

## References

- `references/pipeline-blueprint.md` — the staged recipe, each step with our tool calls
  and concrete pseudo-commands. **Read this to actually run the pipeline.**
- `references/reproduction-plan.md` — the re-planned reproduction: two-box topology
  (Windows + Linux GPU), per-stage tool assignment, phased MVP, risks.
- `references/blender-mcp-setup.md` — **Blender+MCP is live**: launch steps,
  Hunyuan3D/Sketchfab credential wiring, verification & troubleshooting.
- `references/scenes-prompts-and-qa.md` — official 3-stage labels, **11 reusable
  reference prompts**, the **asset-sourcing matrix** (generative vs procedural vs
  Sketchfab), and the **4-channel G-buffer QA pattern** (rgb/instance/normal/depth).
- `references/sam3d-integration.md` — **SAM 3D Objects is released**: API, contract,
  per-object pose fields, Linux/GPU prereqs, SAM License, hybrid (SAM3D pose + Hunyuan mesh).
- `references/agent-architecture.md` — the 7 specialized agents, inputs/outputs, and
  how to emulate each with the coding agent + tools.
- `references/method-details.md` — terrain height-field formula, placement-recovery
  ray-casting, foundation-model matrix, implementation specs (4× H20, Blender 5.1.1,
  2048²/1024² PBR), baselines, limitations.

## Sources

- Paper: https://arxiv.org/abs/2608.05248 (arXiv:2608.05248, 2026)
- Project page: https://tencent-hunyuan.github.io/Hunyuan3D-WorldClaw/
- Code (stub): https://github.com/Tencent-Hunyuan/Hunyuan3D-WorldClaw
- Local full synthesis: `C:\ClaudeProjects\knowledge\worldclaw-3d-openworld.md`
