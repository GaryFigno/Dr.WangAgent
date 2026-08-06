"""Crop receive-code posters to QR-only cards labeled 支付宝 / 微信支付."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "aiharness" / "gui" / "web" / "donate"
ASSETS = Path(
    r"C:\Users\garyf\.cursor\projects\c-ClaudeProjects-AIHarnessAgent\assets"
)

ALIPAY_SRC = ASSETS / (
    "c__Users_garyf_AppData_Roaming_Cursor_User_workspaceStorage_empty-window_"
    "images_ea02cb32f7680d98e3ad7b5c4f20e548-16d044d7-bb53-49b3-b1b2-66cbad6b5e85.png"
)
WECHAT_SRC = ASSETS / (
    "c__Users_garyf_AppData_Roaming_Cursor_User_workspaceStorage_empty-window_"
    "images_937e0214679ce4efe133ecd40b5f3422-cf901271-126f-4c56-9c4c-6d9b14dcca52.png"
)


def is_brand(rgb: tuple[int, int, int], brand: tuple[int, int, int], tol: int = 45) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(rgb, brand, strict=True))


def find_card_box(
    im: Image.Image,
    brand: tuple[int, int, int],
    *,
    y_lo: float,
    y_hi: float,
) -> tuple[int, int, int, int]:
    """Largest non-brand rectangle in the poster mid-band (= white QR card)."""
    pix = im.load()
    w, h = im.size
    y0, y1 = int(h * y_lo), int(h * y_hi)
    x0, x1 = int(w * 0.04), int(w * 0.96)

    # Row is "in card" when enough pixels are not brand color.
    card_rows: list[int] = []
    for y in range(y0, y1):
        non = sum(1 for x in range(x0, x1) if not is_brand(pix[x, y], brand))
        if non / (x1 - x0) >= 0.45:
            card_rows.append(y)
    if len(card_rows) < 30:
        side = int(min(w, h) * 0.5)
        left = (w - side) // 2
        top = int(h * 0.3)
        return (left, top, left + side, top + side)

    best = (card_rows[0], card_rows[0])
    start = prev = card_rows[0]
    for y in card_rows[1:]:
        if y == prev + 1:
            prev = y
            if prev - start > best[1] - best[0]:
                best = (start, prev)
        else:
            start = prev = y
    if prev - start > best[1] - best[0]:
        best = (start, prev)
    top, bot = best

    left, right = w, 0
    for y in range(top, bot + 1, 2):
        for x in range(x0, x1):
            if not is_brand(pix[x, y], brand):
                if x < left:
                    left = x
                if x > right:
                    right = x
    pad = 4
    return (max(0, left - pad), top, min(w, right + pad + 1), min(h, bot + 1))


def crop_qr_from_card(card: Image.Image) -> Image.Image:
    """Keep only the QR matrix; drop the account name under it."""
    pix = card.load()
    w, h = card.size
    # Name sits in the bottom ~12–18% of Alipay/WeChat cards.
    usable = int(h * 0.84)
    # Dark pixel bbox in the usable area (= QR modules)
    top, left, bot, right = usable, w, 0, 0
    found = False
    for y in range(usable):
        for x in range(w):
            r, g, b = pix[x, y]
            if r < 90 and g < 90 and b < 90:
                found = True
                if y < top:
                    top = y
                if x < left:
                    left = x
                if y > bot:
                    bot = y
                if x > right:
                    right = x
    if not found:
        side = int(min(w, usable) * 0.9)
        l = (w - side) // 2
        return card.crop((l, 8, l + side, 8 + side))

    margin = max(6, int(min(bot - top, right - left) * 0.04))
    top = max(0, top - margin)
    left = max(0, left - margin)
    bot = min(usable - 1, bot + margin)
    right = min(w - 1, right + margin)
    side = max(bot - top + 1, right - left + 1)
    # Align to top of QR so we don't pull name text in
    cy = top + (bot - top) // 2
    cx = left + (right - left) // 2
    # Prefer top-aligned square covering the dark bbox
    t = top
    l = max(0, cx - side // 2)
    if t + side > usable:
        t = max(0, usable - side)
    if l + side > w:
        l = max(0, w - side)
    return card.crop((l, t, min(w, l + side), min(h, t + side)))


def labeled_card(qr: Image.Image, label: str, accent: tuple[int, int, int]) -> Image.Image:
    qr = qr.convert("RGB").resize((480, 480), Image.Resampling.NEAREST)
    pad = 24
    label_h = 56
    width = 480 + pad * 2
    height = label_h + 480 + pad * 2
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width, 6), fill=accent)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 28)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, 18), label, fill=(30, 30, 30), font=font)
    canvas.paste(qr, (pad, label_h + pad // 2))
    # Small caption under QR for clarity in the app
    return canvas


def process(
    src: Path,
    label: str,
    brand: tuple[int, int, int],
    accent: tuple[int, int, int],
    dest: Path,
    *,
    y_lo: float,
    y_hi: float,
) -> None:
    im = Image.open(src).convert("RGB")
    box = find_card_box(im, brand, y_lo=y_lo, y_hi=y_hi)
    print(f"{label}: image={im.size} card={box}")
    qr = crop_qr_from_card(im.crop(box))
    print(f"{label}: qr={qr.size}")
    out = labeled_card(qr, label, accent)
    out.save(dest, optimize=True)
    print(f"wrote {dest} ({out.size[0]}x{out.size[1]})")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    process(
        ALIPAY_SRC,
        "支付宝",
        (22, 120, 255),
        (22, 119, 255),
        OUT / "alipay.png",
        y_lo=0.20,
        y_hi=0.90,
    )
    process(
        WECHAT_SRC,
        "微信支付",
        (7, 193, 96),
        (7, 193, 96),
        OUT / "wechat.png",
        y_lo=0.18,
        y_hi=0.78,
    )


if __name__ == "__main__":
    main()
