"""DSH session observation: read the harness's own log as a tributary.

Nodus dispatches runs; it does not sit in front of a conversation a human is
driving. DSH writes one session.v4.jsonl.zstd per conversation, and every
assistant message in it carries the token counts the model reported. This
module reads those messages, prices them with pricing.py, and stores one
observation row per message. The harness keeps its own model path: nothing is
proxied, nothing is rewritten. A second import is a no-op.

The price is an estimate (basis list_price, DeepSeek's official peak/idle
table): the session file records tokens, not money. Only the account meter can
say what was actually deducted.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import config, db, ledger, pricing

HARNESS = "dsh"
PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-flash"
FRAME_GLOB = "sessions/**/session.v4.jsonl.zstd"
DEFAULT_ROOT = r"A:\dsh\home"


def root() -> Path:
    return Path(os.environ.get("NODUS_DSH_HOME") or DEFAULT_ROOT)


def session_files(root_dir: Path | None = None) -> list[Path]:
    base = Path(root_dir) if root_dir is not None else root()
    return sorted(base.glob(FRAME_GLOB))


def _session_id(path: Path) -> str:
    name = path.parent.name
    return name[len("session-"):] if name.startswith("session-") else name


def _project(path: Path) -> str:
    return path.parent.parent.name


def _read_text(path: Path) -> str:
    """Decompress every frame in the session file.

    A session file is many zstd frames appended; the first frame is only the
    header. Prefer the zstandard wheel (present on this account's Python) and
    fall back to zstd.exe so a machine without it still works; if neither
    exists, say so instead of reporting an empty session.
    """
    try:
        import zstandard
    except ImportError:
        exe = shutil.which("zstd") or r"A:\Anaconda\Library\bin\zstd.exe"
        if not exe or not Path(exe).exists():
            raise RuntimeError(
                "no zstd decoder: install zstandard or put zstd on PATH"
            )
        proc = subprocess.run(
            [exe, "-dc", str(path)],
            capture_output=True,
            creationflags=config.NO_WINDOW,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"zstd failed on {path}: {proc.stderr[:200].decode('utf-8', 'replace')}"
            )
        return proc.stdout.decode("utf-8", "replace")
    data = path.read_bytes()
    with zstandard.ZstdDecompressor().stream_reader(
        io.BytesIO(data), read_across_frames=True
    ) as reader:
        return reader.read().decode("utf-8", "replace")


def parse_session(path: Path) -> tuple[str, list[dict]]:
    """(model, messages) from one session file.

    A message is the token counts plus its event time. Tool output is ignored
    on purpose: the ledger needs billing facts, not a transcript copy.
    """
    text = _read_text(path)
    objects: list[dict] = []
    for line in text.splitlines():
        if '"assistant/message"' not in line and '"request/header"' not in line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)

    model = DEFAULT_MODEL
    for obj in objects:
        if obj.get("type") != "request/header":
            continue
        config = ((obj.get("data") or {}).get("header") or {}).get("config") or {}
        if config.get("model"):
            model = str(config["model"])

    messages: list[dict] = []
    for obj in objects:
        if obj.get("type") != "assistant/message":
            continue
        usage = (obj.get("data") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        seq = obj.get("seq")
        stamp = obj.get("time")
        if seq is None or not isinstance(stamp, (int, float)):
            continue
        messages.append(
            {
                "seq": int(seq),
                "at": float(stamp) / 1000.0,
                "input_tokens": int(usage.get("inputTokens") or 0),
                "output_tokens": int(usage.get("outputTokens") or 0),
                "cache_read": int(usage.get("cacheReadTokens") or 0),
                "cache_write": int(usage.get("cacheWriteTokens") or 0),
                "model": model,
            }
        )
    return model, messages


def _observation(path: Path, message: dict) -> dict:
    tokens = ledger.Tokens(
        input=message["input_tokens"],
        output=message["output_tokens"],
        cache_read=message["cache_read"],
        cache_write=message["cache_write"],
    )
    amount, currency, basis, _detail = pricing.price(
        message["model"], PROVIDER, tokens, at=message["at"]
    )
    session_id = _session_id(path)
    return {
        "id": f"obs-{HARNESS}-{session_id}-{message['seq']}",
        "harness": HARNESS,
        "project": _project(path),
        "session_id": session_id,
        "seq": message["seq"],
        "at": message["at"],
        "model": message["model"],
        "provider": PROVIDER,
        "tokens_json": json.dumps(
            {
                "input_tokens": message["input_tokens"],
                "output_tokens": message["output_tokens"],
                "cache_read": message["cache_read"],
                "cache_write": message["cache_write"],
            },
            ensure_ascii=False,
        ),
        "cost": amount,
        "currency": currency,
        "cost_basis": basis,
    }


def _day(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(at))


def scan(
    *,
    dry_run: bool = True,
    force: bool = False,
    root_dir: Path | None = None,
) -> dict:
    """Read every session file and report (or store) what it adds.

    dry_run reads and prices but writes nothing, so it is safe to run against
    the real home. import skips files whose cursor (mtime + size) is unchanged
    and relies on the unique key for the rest.
    """
    base = Path(root_dir) if root_dir is not None else root()
    files = session_files(base)
    summary: dict[str, Any] = {
        "root": str(base),
        "files": len(files),
        "scanned": 0,
        "skipped": 0,
        "messages": 0,
        "inserted": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "cost": {},
        "unpriced": 0,
        "days": {},
    }
    rows: list[dict] = []
    for path in files:
        stat = path.stat()
        key = f"observe:{HARNESS}:{path.relative_to(base).as_posix()}"
        if not dry_run and not force:
            cursor = db.observation_cursor(key)
            if (
                cursor
                and cursor.get("mtime_ns") == stat.st_mtime_ns
                and cursor.get("size") == stat.st_size
            ):
                summary["skipped"] += 1
                continue
        model, messages = parse_session(path)
        summary["scanned"] += 1
        for message in messages:
            row = _observation(path, message)
            rows.append(row)
            tokens = summary["tokens"]
            tokens["input"] += message["input_tokens"]
            tokens["output"] += message["output_tokens"]
            tokens["cache_read"] += message["cache_read"]
            tokens["cache_write"] += message["cache_write"]
            if row["cost"] is None:
                summary["unpriced"] += 1
            else:
                currency = row["currency"] or "?"
                summary["cost"][currency] = round(
                    summary["cost"].get(currency, 0.0) + row["cost"], 6
                )
            day = summary["days"].setdefault(
                _day(message["at"]), {"messages": 0, "tokens": 0, "cost": {}}
            )
            day["messages"] += 1
            day["tokens"] += (
                message["input_tokens"]
                + message["output_tokens"]
                + message["cache_read"]
                + message["cache_write"]
            )
            if row["cost"] is not None:
                currency = row["currency"] or "?"
                day["cost"][currency] = round(
                    day["cost"].get(currency, 0.0) + row["cost"], 6
                )
        if not dry_run:
            db.set_observation_cursor(
                key,
                {
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "session_id": _session_id(path),
                    "last_seq": messages[-1]["seq"] if messages else 0,
                    "model": model,
                },
            )
    summary["messages"] = len(rows)
    if not dry_run:
        summary["inserted"] = db.insert_observations(rows)
    return summary


def observed_runs(
    start: float | None = None, end: float | None = None
) -> list[dict]:
    """Observed messages collapsed into run-shaped rows for the ledger.

    One row per session per local day: the token counts add up exactly, while
    the run count stays meaningful (a session is not 1,500 runs).
    """
    rows = db.observations(start, end)
    buckets: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        try:
            tokens = json.loads(row.get("tokens_json") or "{}")
        except ValueError:
            tokens = {}
        key = (row["session_id"], _day(row["at"]), row["project"] or "")
        bucket = buckets.setdefault(
            key,
            {
                "at": row["at"],
                "model": row["model"],
                "tokens": Counter(),
                "cost": None,
                "currency": None,
                "basis": None,
            },
        )
        bucket["tokens"]["input"] += int(tokens.get("input_tokens") or 0)
        bucket["tokens"]["output"] += int(tokens.get("output_tokens") or 0)
        bucket["tokens"]["cache_read"] += int(tokens.get("cache_read") or 0)
        bucket["tokens"]["cache_write"] += int(tokens.get("cache_write") or 0)
        if row.get("cost") is not None:
            bucket["cost"] = round((bucket["cost"] or 0.0) + float(row["cost"]), 6)
            bucket["currency"] = row.get("currency") or bucket["currency"]
            bucket["basis"] = row.get("cost_basis") or bucket["basis"]

    out: list[dict] = []
    for _key, bucket in sorted(buckets.items(), key=lambda item: item[1]["at"]):
        usage: dict[str, Any] = {
            "input_tokens": bucket["tokens"]["input"],
            "output_tokens": bucket["tokens"]["output"],
            "cache_read": bucket["tokens"]["cache_read"],
            "cache_write": bucket["tokens"]["cache_write"],
        }
        if bucket["cost"] is not None:
            usage["cost"] = bucket["cost"]
            usage["currency"] = bucket["currency"]
            usage["cost_basis"] = bucket["basis"]
        out.append(
            {
                "agent": HARNESS,
                "route": HARNESS,
                "provider": PROVIDER,
                "model": bucket["model"],
                "status": "completed",
                "started_at": bucket["at"],
                "usage_json": json.dumps(usage, ensure_ascii=False),
            }
        )
    return out
