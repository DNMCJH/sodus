"""Trimmed configuration seam for the budget task.

The real Nodus reads these from ``config.HOME``; the exercise keeps only the
two calls ``budget.py`` makes, so the module can run anywhere without a home
directory. The environment overrides are the same idea as the real ones.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("NODUS_HOME") or (Path.home() / ".nodus"))


def load_config() -> dict:
    """Read the workspace config, or an empty dict when it is missing/broken."""
    path = Path(os.environ.get("NODUS_CONFIG") or (HOME / "config.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def workspace_id() -> str:
    """The workspace a run belongs to. Env wins over the config file."""
    env = os.environ.get("NODUS_WORKSPACE")
    if env:
        return env
    return str(load_config().get("workspace") or "default")
