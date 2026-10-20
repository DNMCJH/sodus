"""Agent registry.

Adding a vendor is two things: a spec here, and a parser in `adapters.py`.
Nothing else in the codebase should know vendor names.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass(frozen=True)
class Pricing:
    """List price per million tokens, in `currency`.

    A vendor that resells another vendor's CLI reports the *host* vendor's
    price. When we know the real list price, we compute instead of believing.
    """

    input: float
    output: float
    cache_read: float = 0.0
    currency: str = "USD"

    def cost(self, usage: dict) -> float:
        return round(
            (
                int(usage.get("input_tokens") or 0) * self.input
                + int(usage.get("output_tokens") or 0) * self.output
                + int(usage.get("cache_read") or 0) * self.cache_read
            )
            / 1_000_000,
            6,
        )


@dataclass
class AgentSpec:
    name: str
    # Which CLI and which stream format. A vendor may reuse another's CLI.
    vendor: str
    command: list[str]
    config_home: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    # env var -> secret name, injected at run time so keys never hit disk here
    env_secrets: dict[str, str] = field(default_factory=dict)
    needs_env: list[str] = field(default_factory=list)
    default_model: str | None = None
    pricing: Pricing | None = None
    #: which billing meter to read around a run: "relay" / "deepseek" / None
    meter: str | None = None
    #: a local gateway parses this agent's session files and can price each call
    gateway_sessions: bool = False
    can_resume_session: bool = False


#: Claude Code's headless contract. MiniMax serves the same stream format.
CLAUDE_ARGS = [
    "claude",
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--permission-mode",
    "bypassPermissions",
]

MINIMAX_BASE = os.environ.get("NODUS_MINIMAX_BASE", "https://api.minimax.cn/anthropic")
MINIMAX_MODEL = os.environ.get("NODUS_MINIMAX_MODEL", "MiniMax-M3.1-Flash")


def _specs() -> dict[str, AgentSpec]:
    return {
        "claude": AgentSpec(
            name="claude",
            vendor="claude",
            command=list(CLAUDE_ARGS),
            config_home=config.AGENTS_DIR / "claude",
            meter="relay",
        ),
        "minimax": AgentSpec(
            name="minimax",
            vendor="claude",  # same CLI, same parser; only the backend differs
            command=list(CLAUDE_ARGS),
            config_home=config.AGENTS_DIR / "minimax",
            env_secrets={"ANTHROPIC_AUTH_TOKEN": "MINIMAX_API_KEY"},
            needs_env=["MINIMAX_API_KEY"],
            default_model=MINIMAX_MODEL,
            pricing=Pricing(input=1.0, output=4.0, cache_read=0.1, currency="CNY"),
        ),
        "codex": AgentSpec(
            name="codex",
            vendor="codex",
            command=[
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
            ],
            config_home=config.AGENTS_DIR / "codex",
        ),
        "dsh": AgentSpec(
            name="dsh",
            vendor="dsh",
            command=[
                os.environ.get("NODUS_NODE", "node"),
                os.environ.get(
                    "NODUS_DSH_BIN",
                    r"A:\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js",
                ),
                "headless",
                "--json",
                "-",
            ],
            needs_env=["DEEPSEEK_API_KEY"],
            # DSH keeps its sessions in a location the gateway already tails.
            # Claude Code here runs from an isolated config dir it does not scan.
            gateway_sessions=True,
            meter="deepseek",
            can_resume_session=True,
        ),
    }


SPECS = _specs()


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def bootstrap_claude() -> None:
    """Create an isolated Claude config dir from the user's existing settings."""
    import json

    target = config.AGENTS_DIR / "claude"
    target.mkdir(parents=True, exist_ok=True)
    settings_path = target / "settings.json"
    if settings_path.exists():
        return
    user = Path.home() / ".claude" / "settings.json"
    if not user.exists():
        return
    raw = json.loads(user.read_text(encoding="utf-8"))
    keep = {k: raw[k] for k in ("env", "model", "modelSettings") if k in raw}
    settings_path.write_text(json.dumps(keep, indent=2, ensure_ascii=False), encoding="utf-8")


def bootstrap_codex() -> None:
    """Create an isolated Codex home with only the model/provider settings."""
    import tomllib

    target = config.AGENTS_DIR / "codex"
    target.mkdir(parents=True, exist_ok=True)
    config_path = target / "config.toml"
    if config_path.exists():
        return
    user = Path.home() / ".codex" / "config.toml"
    if not user.exists():
        return
    data = tomllib.loads(user.read_text(encoding="utf-8"))
    model = data.get("model")
    provider = data.get("model_provider")
    providers = data.get("model_providers", {})
    lines: list[str] = []
    if model:
        lines.append(f'model = "{_toml_escape(str(model))}"')
    if provider:
        lines.append(f'model_provider = "{_toml_escape(str(provider))}"')
    if provider and provider in providers:
        lines.append("")
        lines.append(f"[model_providers.{provider}]")
        for k, v in providers[provider].items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f'{k} = "{_toml_escape(str(v))}"')
    if lines:
        windows = data.get("windows") or {}
        if windows.get("sandbox"):
            lines.append("")
            lines.append("[windows]")
            lines.append(f'sandbox = "{_toml_escape(str(windows["sandbox"]))}"')
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bootstrap_minimax() -> None:
    """Point an isolated Claude home at MiniMax's Anthropic-compatible API.

    The token is deliberately absent here: it is injected per run from the
    secrets file, so rotating a key never means editing this file.
    """
    import json

    target = config.AGENTS_DIR / "minimax"
    target.mkdir(parents=True, exist_ok=True)
    settings_path = target / "settings.json"
    if settings_path.exists():
        return
    settings = {
        "model": MINIMAX_MODEL,
        "env": {
            "ANTHROPIC_BASE_URL": MINIMAX_BASE,
            "ANTHROPIC_MODEL": MINIMAX_MODEL,
            # Claude Code resolves aliases for its own model tiers; without
            # this a background call could ask MiniMax for a claude-* model.
            "ANTHROPIC_DEFAULT_OPUS_MODEL": MINIMAX_MODEL,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": MINIMAX_MODEL,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": MINIMAX_MODEL,
        },
    }
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


def registry() -> dict[str, AgentSpec]:
    config.ensure_dirs()
    bootstrap_claude()
    bootstrap_minimax()
    bootstrap_codex()
    return SPECS


def get(name: str) -> AgentSpec | None:
    return registry().get(name)


def missing_requirements(spec: AgentSpec, secrets: dict[str, str]) -> list[str]:
    missing = [k for k in spec.needs_env if not secrets.get(k)]
    if spec.vendor != "dsh" and not shutil.which(spec.command[0]):
        missing.append(f"command not found: {spec.command[0]}")
    return missing


def ready(name: str, secrets: dict[str, str] | None = None) -> bool:
    spec = registry().get(name)
    if spec is None:
        return False
    return not missing_requirements(spec, secrets or config.load_secrets())


def build_env(spec: AgentSpec, secrets: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    # DSH and friends strip *_KEY from inherited env; inject explicitly.
    for key, value in secrets.items():
        env[key] = value
    for env_name, secret_name in spec.env_secrets.items():
        value = secrets.get(secret_name)
        if not value:
            continue
        env[env_name] = value
        if env_name == "ANTHROPIC_AUTH_TOKEN":
            # Claude Code prefers x-api-key when an Anthropic key is inherited;
            # that would quietly outrank the token this spec just supplied.
            env.pop("ANTHROPIC_API_KEY", None)
    if spec.config_home is not None:
        if spec.vendor == "claude":
            env["CLAUDE_CONFIG_DIR"] = str(spec.config_home)
        elif spec.vendor == "codex":
            env["CODEX_HOME"] = str(spec.config_home)
    env.update(spec.env)
    env["NODUS_RUN"] = "1"
    return env


def argv(spec: AgentSpec, effort: str | None = None, session_id: str = "") -> list[str]:
    """Resolve the executable. npm installs .cmd shims on Windows, and
    CreateProcess cannot launch those directly."""
    cmd = list(spec.command)
    if spec.default_model:
        # Pin the model on the command line so the run record names what ran,
        # instead of trusting whatever the CLI config resolves to.
        cmd += ["--model", spec.default_model]
    if effort:
        if spec.vendor == "claude":
            cmd += ["--effort", effort]
        elif spec.vendor == "codex":
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        # dsh has no per-run effort flag; its effort lives in the profile config.
    if session_id:
        if not spec.can_resume_session:
            raise ValueError(f"{spec.vendor} cannot resume a DSH session")
        # dsh reads the prompt from "-", so the flag must come before it
        cmd = cmd[:-1] + ["--session-id", session_id, cmd[-1]]
    exe = shutil.which(cmd[0])
    if not exe:
        return cmd
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe, *cmd[1:]]
    return [exe, *cmd[1:]]
