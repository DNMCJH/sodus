"""Calibrate this relay's per-token prices from controlled runs.

Why this exists: the relay publishes a price table (`/api/pricing`), but its
numbers do not fit the classic ratio formula — fitting that formula to measured
runs produced a negative cache-write coefficient. What *does* fit is a linear
model over output / cache-read / cache-write tokens, once the shapes are varied
one at a time.

Method:
  * one model, one account, strictly sequential (no other agent may be running)
  * repeat one shape to see the noise floor before trusting anything
  * vary output alone, context alone, and both
  * read the account meter before and after each run; the delta belongs to
    that run only because nothing else was running
  * solve least squares, then check a held-out shape

A model that does not fit is not shipped: the sonnet data in the first
experiment left 13% residuals and a negative intercept, and it stays
uncalibrated.

    python scripts/calibrate_relay.py --model "claude-opus-5-5[1M]"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodus import config, db, meter, providers  # noqa: E402

FILLER = "以下是一段需要忽略的填充文本。" * 120
CASES = [
    ("plain", "只回复两个字：好的", ""),
    ("plain2", "只回复两个字：好的", ""),
    ("context", "只回复两个字：好的", FILLER),
    ("short-out", "写 12 行编号列表，每行一个常见英文单词，不要别的", ""),
    ("long-out", "写 24 行编号列表，每行一个常见英文单词，不要别的", ""),
    ("both", "写 12 行编号列表，每行一个常见英文单词，不要别的", FILLER),
]


def balance(provider, secrets) -> float:
    reading = meter._relay(provider, secrets)
    return reading.value if reading else float("nan")


def run_once(task_id: str, work: Path, note: str, model: str) -> dict:
    subprocess.run(
        [sys.executable, "-c",
         "import sys; from nodus.cli import main; sys.exit(main(sys.argv[1:]))",
         "run", task_id, "--route", "claude", "--model", model, "--no-verify",
         "--cwd", str(work), "--note", note],
        capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace",
    )
    time.sleep(6)  # billing lands asynchronously
    rid = db.list_runs(task_id, limit=1)[0]["id"]
    usage = json.loads(db.get_run(rid).get("usage_json") or "{}")
    return {
        "run": rid,
        "in": int(usage.get("input_tokens") or 0),
        "out": int(usage.get("output_tokens") or 0),
        "cr": int(usage.get("cache_read") or 0),
        "cw": int(usage.get("cache_write") or 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="claude-opus-5-5[1M]")
    parser.add_argument("--cases", type=int, default=len(CASES))
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    import numpy as np

    secrets = config.load_secrets()
    relay = providers.load()["relay"]
    work = Path(config.HOME) / "calibration"
    work.mkdir(parents=True, exist_ok=True)
    task = db.create_task(f"calibration {args.model}")

    rows = []
    for name, note, filler in CASES[: args.cases]:
        full = f"{filler}\n\n{note}" if filler else note
        before = balance(relay, secrets)
        row = run_once(task["id"], work, full, args.model)
        after = balance(relay, secrets)
        row.update({"case": name, "cost": round(after - before, 6)})
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    X = np.array([[1.0, r["out"], r["cr"], r["cw"]] for r in rows])
    y = np.array([r["cost"] for r in rows])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    rms = float(np.sqrt(np.mean(resid**2)))
    mean = float(np.mean(y))
    print(f"\nrms residual {rms:.6f} ({100*rms/mean:.1f}% of mean cost)")

    k, per_out, per_cr, per_cw = (float(v) for v in coef)
    if min(per_out, per_cr, per_cw) < 0:
        print("REFUSING: a negative coefficient means the model is wrong for this "
              "endpoint; nothing was written")
        return 1
    if rms / mean > 0.05:
        print(f"REFUSING: {100*rms/mean:.1f}% residual is too large to call this a price model")
        return 1

    block = {
        args.model: {
            "per_output": per_out,
            "per_cache_read": per_cr,
            "per_cache_write": per_cw,
            "per_call": k,
            "measured": time.strftime("%Y-%m-%d"),
        }
    }
    print("\nadd this to ~/.nodus/config.json under cost_models:\n")
    print(json.dumps({"cost_models": block}, indent=2)[1:-1])
    if args.out:
        Path(args.out).write_text(json.dumps(block, indent=2), encoding="utf-8")
        print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
