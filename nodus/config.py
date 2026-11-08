"""Settings: where state lives, who we are, what secrets we have.

Agent definitions and argv live in `agents.py`; this module must not import it
(agents is the higher layer).
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from . import __version__ as VERSION

#: The implementation-name home. Still the default: phase C1 renamed nothing on
#: disk, so `nodus` and `sodus` keep reading one database until a migration runs.
DEFAULT_HOME = Path.home() / ".nodus"
#: The product-name home. Only used once it exists AND no env var overrides it.
SODUS_HOME_DEFAULT = Path.home() / ".sodus"
LEGACY_HOME = Path.home() / ".workbench"


def _resolve_home() -> Path:
    """SODUS_HOME > NODUS_HOME > an existing ~/.sodus > ~/.nodus.

    The order is what keeps the two console scripts on one database: without
    `~/.sodus` on disk, `sodus` reads the same `~/.nodus` `nodus` always used.
    Nothing is created here -- `migrate-home` is the only thing that writes a
    `~/.sodus`, and it is never run automatically.
    """
    override = os.environ.get("SODUS_HOME") or os.environ.get("NODUS_HOME")
    if override:
        return Path(override).expanduser()
    if SODUS_HOME_DEFAULT.exists():
        return SODUS_HOME_DEFAULT
    return DEFAULT_HOME


HOME = _resolve_home()
#: Pre-0.4 per-agent config dirs. Read once by the provider/profile migration.
LEGACY_AGENTS_DIR = HOME / "agents"


#: Pass to every subprocess so a console-less daemon does not flash a black
#: window on the desktop each time it shells out. `netstat` runs on every pet
#: lookup, so without this the desktop blinks every couple of minutes.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def home_id() -> str:
    """A short, stable fingerprint of this state directory.

    Two `nodus` processes can listen on the same loopback port while reading
    different databases — a verification run with its own NODUS_HOME is enough.
    The board then shows somebody else's work and every decision made from it is
    wrong. `nodus up` compares this fingerprint before claiming "already up".
    """
    import hashlib

    return hashlib.sha256(str(HOME.resolve()).encode("utf-8")).hexdigest()[:12]


def _absorb_legacy_home() -> None:
    """Move a pre-rename state directory instead of silently starting empty.

    Only when the caller did not pick a home explicitly: an operator who sets
    NODUS_HOME is pointing at a specific place on purpose.
    """
    if (
        os.environ.get("SODUS_HOME")
        or os.environ.get("NODUS_HOME")
        or HOME.exists()
        or not LEGACY_HOME.exists()
    ):
        return
    try:
        LEGACY_HOME.rename(HOME)
    except OSError:
        return
    legacy_db = HOME / "wb.sqlite3"
    if legacy_db.exists() and not (HOME / "nodus.sqlite3").exists():
        legacy_db.rename(HOME / "nodus.sqlite3")
    for suffix in ("-wal", "-shm"):
        old = HOME / f"wb.sqlite3{suffix}"
        if old.exists():
            old.rename(HOME / f"nodus.sqlite3{suffix}")


_absorb_legacy_home()


def workspace_id() -> str:
    return os.environ.get("NODUS_WORKSPACE", "ws-default")


def state_dir(ws: str | None = None) -> Path:
    return HOME / "workspaces" / (ws or workspace_id())


def token() -> str:
    """Local API token. Generated once, readable only by the user."""
    ensure_dirs()
    path = HOME / "token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(24)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def active_profile() -> str:
    """Which profile new runs use. Stored here because it is a setting, not a
    capability: `profiles` owns the contents, `config` owns the pointer."""
    return str(load_config().get("profile") or "default")


def default_route() -> str:
    """Which route a bare `nodus run` uses when the caller names none.

    A setting, not a capability: the route table owns what exists, this owns
    what is picked by default. Kept out of `routes.py` so the default can be
    changed without editing the route table, and so a benchmark/replay that
    loads routes from a scratch home still resolves the operator's default.
    """
    routing = load_config().get("routing") or {}
    if isinstance(routing, dict):
        value = routing.get("default")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "claude"


def ensure_dirs() -> None:
    for d in (HOME, HOME / "profiles", HOME / "tasks", HOME / "runs", HOME / "briefs", state_dir()):
        d.mkdir(parents=True, exist_ok=True)


def config_path() -> Path:
    return HOME / "config.json"


def load_config() -> dict:
    """User settings. Unknown keys are ignored so old files keep working."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def session_roots() -> list[Path]:
    """Optional read whitelist for DSH sessions. Empty means "no restriction"."""
    raw = load_config().get("session_roots") or []
    if isinstance(raw, str):
        raw = [raw]
    roots = []
    for item in raw:
        try:
            roots.append(Path(item).expanduser().resolve())
        except (OSError, ValueError):
            continue
    return roots


def gateway_currency() -> str:
    """Currency the local gateway's `cost` column is denominated in.

    Verified by reading the gateway's own report (`magpie usage` prints `≈$`),
    not assumed: a wrong unit here is a silent 7x error. Configurable because
    the gateway can be pointed at any provider set.
    """
    raw = os.environ.get("NODUS_MAGPIE_CURRENCY") or load_config().get("magpie_currency")
    return str(raw or "USD").upper()


def cost_rate(unit: str) -> float:
    """CNY per meter unit.

    A relay bills in its own balance unit, which its docs say is not USD: the
    displayed price is "site balance" = official price x group multiplier, and
    on this account one balance unit is bought with one yuan. That exchange
    rate is an account fact the API never states, so it is configuration, not
    a constant.
    """
    defaults = {"cny": 1.0, "relay_unit": 1.0}
    raw = (load_config().get("cost_rates") or {}).get(unit)
    try:
        return float(raw) if raw is not None else defaults.get(unit, 1.0)
    except (TypeError, ValueError):
        return defaults.get(unit, 1.0)


def auto_verify() -> bool:
    """Whether a run that changed files should run the project's tests.

    On by default: an acceptance layer that reports "completed" without ever
    executing anything is the thing this project exists to fix. Off is one
    config key away, and `nodus run --no-verify` covers the single case.
    """
    raw = load_config().get("auto_verify")
    return True if raw is None else bool(raw)


def load_secrets(ws: str | None = None) -> dict[str, str]:
    """Secrets are per-workspace, falling back to the shared file."""
    secrets_map: dict[str, str] = {}
    candidates = [HOME / "secrets.json"]
    per_ws = state_dir(ws) / "secrets.json"
    if per_ws not in candidates:
        candidates.append(per_ws)  # per-workspace wins
    for path in candidates:
        if not path.exists():
            continue
        try:
            secrets_map.update(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    for key in ("DEEPSEEK_API_KEY", "TYPESAFE_API_KEY"):
        if os.environ.get(key):
            secrets_map[key] = os.environ[key]
    return secrets_map


def dream_route() -> str:
    value = (load_config().get("routing") or {}).get("dream")
    return value.strip() if isinstance(value, str) and value.strip() else default_route()


def dream_daily_tokens() -> int:
    return max(0, int((load_config().get("memory") or {}).get("dream_daily_tokens", 100000)))
