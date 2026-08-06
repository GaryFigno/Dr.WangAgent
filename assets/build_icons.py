"""Build the application icon set from the source illustration.

Two things matter here, and getting either wrong produces the brown smudge
that made an earlier attempt at this look impossible:

**The source must be composed for an icon.** The subject fills the frame,
the silhouette carries a dark outline, and contrast is pushed harder than
looks right at full size. Detailed illustration works fine as an icon — it
just has to be drawn for the job rather than cropped into it.

**The downscale must be progressive.** Going from 1024 to 32 in one LANCZOS
step throws away exactly the edges that carry the shape. Halving repeatedly
and finishing with an unsharp mask keeps them.

Run:  python assets/build_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

#: The illustration this is built from, relative to this file.
SOURCE = "gen/icon_v2_1.png"
#: Working resolution; every output is derived from this.
MASTER_SIZE = 1024
#: Sizes written as standalone PNGs.
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
#: Sizes packed into the Windows .ico.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
#: Squircle radius as a fraction of the canvas, matching platform icon masks.
CORNER_RATIO = 0.227
#: Plate colour behind the artwork, matching the zhaocai theme background.
PLATE_COLOUR = (28, 25, 23, 255)

#: At or below this size, sharpen and lift contrast to survive the shrink.
SMALL_SIZE_CUTOFF = 64
SMALL_CONTRAST_BOOST = 1.18
UNSHARP_PERCENT = 140
UNSHARP_THRESHOLD = 2
#: Divisor turning a target size into an unsharp radius.
UNSHARP_RADIUS_DIVISOR = 32

OUTPUT_DIR = Path(__file__).parent


def build_master(source: Path) -> Image.Image:
    """Load the illustration and mask it into a rounded square."""
    art = Image.open(source).convert("RGBA").resize(
        (MASTER_SIZE, MASTER_SIZE), Image.LANCZOS
    )
    mask = Image.new("L", (MASTER_SIZE, MASTER_SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, MASTER_SIZE - 1, MASTER_SIZE - 1),
        radius=int(MASTER_SIZE * CORNER_RATIO),
        fill=255,
    )
    plate = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), PLATE_COLOUR)
    plate.paste(art, (0, 0))
    plate.putalpha(mask)
    return plate


def downscale(image: Image.Image, size: int) -> Image.Image:
    """Resize to ``size`` without destroying the silhouette.

    Args:
      image: The master image.
      size: Target edge length in pixels.

    Returns:
      The resized image, sharpened when small enough to need it.
    """
    current = image
    while current.width // 2 >= size:
        current = current.resize((current.width // 2, current.height // 2), Image.LANCZOS)
    if current.width != size:
        current = current.resize((size, size), Image.LANCZOS)

    if size <= SMALL_SIZE_CUTOFF:
        current = ImageEnhance.Contrast(current).enhance(SMALL_CONTRAST_BOOST)
        current = current.filter(
            ImageFilter.UnsharpMask(
                radius=max(size // UNSHARP_RADIUS_DIVISOR, 1),
                percent=UNSHARP_PERCENT,
                threshold=UNSHARP_THRESHOLD,
            )
        )
    return current


def build(output_dir: Path = OUTPUT_DIR) -> list[Path]:
    """Write every icon asset.

    Returns:
      The paths written.

    Raises:
      FileNotFoundError: If the source illustration is missing.
    """
    source = output_dir / SOURCE
    if not source.exists():
        raise FileNotFoundError(
            f"{source} is missing. It is the generated illustration the icons "
            f"are built from; regenerate it or point SOURCE at another file."
        )

    master = build_master(source)
    written: list[Path] = []

    for size in PNG_SIZES:
        path = output_dir / f"icon-{size}.png"
        downscale(master, size).save(path)
        written.append(path)

    logo = output_dir / "logo.png"
    master.save(logo)
    written.append(logo)

    ico = output_dir / "icon.ico"
    master.save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    written.append(ico)
    return written


if __name__ == "__main__":
    for path in build():
        print(path)
