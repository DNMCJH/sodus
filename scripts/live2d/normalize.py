"""Normalize captured 960x540 RGBA frames so the character occupies 75% of the
canvas height with its bottom edge at y=495, matching existing dsh-pet characters.
No new dependencies: Pillow only."""
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(r"C:\Users\35336\AppData\Local\Temp\live2d-out")
RAW = ROOT / "raw"
DST = ROOT / "frames"
CANVAS = (960, 540)
TARGET_H = 405          # 75% of 540
BOTTOM_Y = 495
CENTER_X = 480


def frames(d):
    return sorted(Path(d).glob("*.png"))


def bbox_of(img):
    return img.getchannel("A").point(lambda v: 255 if v > 0 else 0).getbbox()


def alpha_stats(img):
    a = img.getchannel("A")
    hist = a.histogram()
    total = img.width * img.height
    return {
        "opaque_px": sum(hist[1:]),
        "coverage": round(sum(hist[1:]) / total, 4),
        "min": min(i for i, v in enumerate(hist) if v),
        "max": max(i for i, v in enumerate(hist) if v),
        "transparent_px": hist[0],
    }


def main(clip):
    src = frames(RAW / clip)
    if not src:
        raise SystemExit(f"no frames in {RAW / clip}")
    ref = Image.open(src[0]).convert("RGBA")
    if ref.size != CANVAS:
        print(f"WARN unexpected size {ref.size}")
    bb = bbox_of(ref)
    st = alpha_stats(ref)
    print(f"[{clip}] frames={len(src)} size={ref.size} mode={ref.mode} bbox={bb} alpha={st}")
    if not bb:
        raise SystemExit("frame 0 has no visible pixels")

    bx0, by0, bx1, by1 = bb
    content_h = by1 - by0
    s = TARGET_H / content_h
    pad = round(content_h * 0.08)
    cx0, cy0 = max(0, bx0 - pad), max(0, by0 - pad)
    cx1, cy1 = min(ref.width, bx1 + pad), min(ref.height, by1 + pad)

    out = DST / clip
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()

    prev = None
    diffs = []
    empty = 0
    for i, path in enumerate(src):
        img = Image.open(path).convert("RGBA")
        crop = img.crop((cx0, cy0, cx1, cy1))
        nw, nh = round(crop.width * s), round(crop.height * s)
        crop = crop.resize((nw, nh), Image.LANCZOS)
        canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        px = round(CENTER_X - ((bx0 + bx1) / 2 - cx0) * s)
        py = round(BOTTOM_Y - (by1 - cy0) * s)
        canvas.alpha_composite(crop, (px, py))
        bb2 = bbox_of(canvas)
        if bb2 is None:
            empty += 1
        canvas.save(out / f"{i:04d}.png", optimize=False, compress_level=3)
        g = ImageStat.Stat(canvas.convert("LA"))
        if prev is not None:
            diffs.append(round(sum(ImageStat.Stat(ImageChops.difference(prev, canvas)).mean), 3))
        prev = canvas

    final = Image.open(out / f"{len(src) - 1:04d}.png")
    print(f"[{clip}] out={out} written={len(src)} empty={empty} "
          f"bbox_last={bbox_of(final)} scale={round(s, 4)} "
          f"paste=({px},{py}) crop=({cx0},{cy0},{cx1},{cy1})")
    if diffs:
        nz = [d for d in diffs if d > 0]
        print(f"[{clip}] frame-diff min={min(diffs)} max={max(diffs)} mean={round(sum(diffs) / len(diffs), 3)} moving_pairs={len(nz)}/{len(diffs)}")
    return {"clip": clip, "frames": len(src), "scale": round(s, 4), "bbox": bb,
            "alpha": st, "crop": [cx0, cy0, cx1, cy1], "diff_max": max(diffs) if diffs else 0}


if __name__ == "__main__":
    clips = sys.argv[1:] or ["idle", "react"]
    res = [main(c) for c in clips]
    (ROOT / "normalize-report.json").write_text(json.dumps(res, indent=2), encoding="utf-8")

