"""Contact sheets for review: 6 evenly spaced frames per clip, composited on a
dark checkerboard so the alpha channel is visible, with a time label."""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(r"C:\Users\35336\AppData\Local\Temp\live2d-out")
CELL = (320, 180)
COLS = 3
PAD = 8


def checker(w, h, s=12, a=(38, 38, 44), b=(52, 52, 60)):
    img = Image.new("RGB", (w, h), a)
    d = ImageDraw.Draw(img)
    for y in range(0, h, s):
        for x in range(0, w, s):
            if (x // s + y // s) % 2 == 0:
                d.rectangle([x, y, x + s - 1, y + s - 1], fill=b)
    return img


def sheet(clip, label):
    src = sorted((ROOT / "frames" / clip).glob("*.png"))
    n = len(src)
    picks = [round(i * (n - 1) / 5) for i in range(6)]
    rows = 2
    W = COLS * CELL[0] + (COLS + 1) * PAD
    H = rows * CELL[1] + (rows + 1) * PAD + 26
    out = Image.new("RGB", (W, H), (22, 22, 26))
    d = ImageDraw.Draw(out)
    d.text((PAD, 6), f"{label}  ({n} frames @24fps, {n/24:.2f}s)   frame -> time", fill=(220, 220, 230))
    for k, idx in enumerate(picks):
        im = Image.open(src[idx]).convert("RGBA").resize(CELL, Image.LANCZOS)
        bg = checker(*CELL)
        bg.paste(im, (0, 0), im)
        c, r = k % COLS, k // COLS
        x = PAD + c * (CELL[0] + PAD)
        y = 26 + PAD + r * (CELL[1] + PAD)
        out.paste(bg, (x, y))
        d.rectangle([x, y, x + CELL[0] - 1, y + CELL[1] - 1], outline=(90, 90, 100))
        d.text((x + 4, y + 4), f"#{idx}  {idx/24:.2f}s", fill=(255, 235, 120))
    p = ROOT / f"sheet-{clip}.png"
    out.save(p)
    print(f"{p}  picks={picks}")
    return p


sheet("idle", "IDLE  breath + blink")
sheet("react", "REACTION  tail-cozy-sway")
