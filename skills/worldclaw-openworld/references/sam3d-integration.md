# SAM 3D Objects — released component for WorldClaw's Stage 3

> Status: **released** (code + gated weights + callable API), verified 2026-08-12.
> This corrects the earlier "SAM3/SAM3D not released — substitute with crop +
> hunyuan3d" note. SAM 3D Objects is the real thing WorldClaw's "SAM3D" stage names.

## What it is

`facebookresearch/sam-3d-objects` — Meta FAIR's foundation model that **reconstructs
full 3D shape geometry, texture, and layout from a single image**, robust to
occlusion and clutter. It is the "objects" half of SAM 3D (the other half,
`sam-3d-body`, reconstructs humans). Paper: *SAM 3D: 3Dfy Anything in Images*
(arXiv **2511.16624**). Trained on ~1M images / ~3.14M model-in-the-loop meshes;
≥5:1 human-preference win rate over prior 3D generation models.

## Why it maps exactly onto WorldClaw Stage 3

WorldClaw's Stage 3 needs: from a region composition image, extract each object and
recover a **mesh + per-object camera transform** for placement. SAM 3D Objects'
contract is precisely this:

- **Input:** one RGB image **+ a per-object binary mask** (use SAM/SAM2/SAM3 to get
  the masks — fits the "SAM" heritage).
- **Output:** geometry + texture + **pose, shape, texture, layout** — i.e. an
  explicit **per-object transform** in a shared scene frame, which is exactly what
  placement recovery consumes.

So it replaces both the SAM3 (segmentation) and SAM3D (single-view recon + pose)
roles in one call, and outputs the per-object transform WorldClaw then uses to land
the asset on the terrain.

## API (verbatim from the repo)

```python
import sys
sys.path.append("notebook")
from inference import Inference, load_image, load_single_mask

tag = "hf"
config_path = f"checkpoints/{tag}/pipeline.yaml"
inference = Inference(config_path, compile=False)

image = load_image("notebook/images/<scene>/image.png")
mask = load_single_mask("notebook/images/<scene>", index=14)

output = inference(image, mask, seed=42)
output["gs"].save_ply("splat.ply")          # native output = Gaussian splat .ply
```

Per-object fields returned (the placement-recovery inputs):
- `output["gs"]` / `["gaussian"]` — the object's 3D Gaussian splat
- `output["rotation"]` — quaternion (local → camera/scene)
- `output["translation"]`
- `output["scale"]`

Multi-object scene assembly: `make_scene(*outputs)` merges per-object gaussians into
one world frame using those `rotation/translation/scale` fields → `<scene>_posed.ply`.

## Native output is a splat, not a mesh — opt in for textured mesh

Default export is a **Gaussian splat `.ply`**. For a textured mesh (what downstream
engine/Blender stages usually want), enable the postprocess flags on the inference
call: `with_mesh_postprocess=True`, `with_texture_baking=True` (FlexiCubes /
`cube2mesh` decoder ships in the package). Plan a splat→mesh conversion if you keep
the default.

## Hard prerequisites (the real constraint for us)

- **Platform: Linux-64 only.** Our current box is **win32** → SAM 3D Objects will
  NOT run here natively. Options: WSL2 with GPU passthrough, or a Linux GPU node.
- **GPU: ≥ 32 GB VRAM** minimum (`doc/setup.md`).
- **Heavy CUDA stack, install-from-source, no pip package:** PyTorch3D (from git),
  Kaolin 0.17.0 (from NVIDIA S3, pinned `torch-2.5.1_cu121`), `gsplat`,
  `flash_attn==2.8.3`, spconv, plus a local **hydra patch** (`./patching/hydra`).
  CUDA 12.1 / PyTorch 2.5.1. Conda env via `environments/default.yml`.
- **Weights are gated** on HuggingFace `facebook/sam-3d-objects` — request access,
  `hf auth login`, `hf download --local-dir checkpoints/hf-download ...`. Auto-
  approved outside comprehensively sanctioned jurisdictions.

## License

Custom **"SAM License"** (SPDX `NOASSERTION`, like Meta's Llama licenses). Broad
royalty-free grant including commercial use and patent cover for sell/import — but:
no military/warfare/nuclear/espionage/guns end-uses; US/EU/UK export-control
compliance; no reverse engineering; pass-through on redistribution. **Commercial
ship needs legal sign-off.**

## Recommended strategy: SAM3D pose + Hunyuan3D mesh (hybrid)

Two viable modes for WorldClaw Stage 3:

1. **All-SAM3D** — let SAM 3D Objects produce geometry + texture + pose. Simplest
   pose/layout; quality is SOTA on cluttered natural images; free to run locally.
2. **Hybrid (recommended for max fidelity)** — use SAM 3D Objects for **detection +
   per-object pose/layout** (its `rotation/translation/scale`), then feed each
   object's crop to **`hunyuan3d-pipeline` `gen`** for the final high-fidelity
   textured mesh, placed at the pose SAM3D recovered. Best of both: SAM3D's scene-
   aware placement + Hunyuan3D's mesh/PBR quality.

## On Windows (our current box) — keep the fallback

Until we have a Linux+GPU node, Stage 3's object reconstruction still runs the
documented fallback: **crop each object from the composition image + run
`hunyuan3d-pipeline` `gen`** (collapses seg+recon into one, but loses SAM3D's
per-object pose — you recover placement via the ray-casting in `method-details.md`).
Switch to SAM 3D Objects once a Linux GPU node is available.

## Sources

- Repo: https://github.com/facebookresearch/sam-3d-objects
- Weights (gated): https://huggingface.co/facebook/sam-3d-objects
- Paper: https://arxiv.org/abs/2511.16624
- Blog: https://ai.meta.com/blog/sam-3d/
- Web demo: https://www.aidemos.meta.com/segment-anything/editor/convert-image-to-3d
- Benchmark leaderboard: https://huggingface.co/spaces/facebook/sa3dao-leaderboard
