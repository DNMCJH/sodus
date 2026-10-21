"""Agents: the CLI that runs, not the API it reaches.

An `AgentSpec` answers three questions and nothing else:

    which executable do I start   command
    how do I speak to it          dialect (argument flags) + parser (adapters.py)
    what can it do                resume a session, be priced by the gateway

It does **not** know URLs, tokens or prices. Those belong to a provider
(providers.py), and the pairing of one agent with an ordered list of providers
is a route (routes.py). Adding a vendor touches this file and `adapters.py`;
adding a backend touches neither.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import providers

#: Claude Code's headless contract. MiniMax serves the same stream format, and
#: so does anything else that implements the Anthropic protocol.
CLAUDE_ARGS = [
    "claude",
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--permission-mode",
    "bypassPermissions",
]

#: opencode's headless contract. Two differences from the others matter:
#: the message is a positional argument (not stdin), and the endpoint plus key
#: come from its own config file, so the provider on the route only decides
#: what Nodus meters the run against.
OPENCODE_ARGS = ("opencode", "run", "--format", "json")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    #: stream parser + argument dialect, i.e. "which CLI contract is this"
    vendor: str
    command: tuple[str, ...]
    #: protocols this CLI can be pointed at
    accepts: tuple[str, ...]
    #: extra secrets beyond the provider's own key, by name in secrets.json
    secrets: tuple[str, ...] = ()
    #: how this CLI continues an existing conversation: "" | "flag" | "subcommand"
    resume_mode: str = ""
    #: how it branches one; empty means the vendor has no native fork
    fork_mode: str = ""
    #: a local gateway tails this CLI's session files and can price each call
    gateway_sessions: bool = False

    def accepts_protocol(self, protocol: str) -> bool:
        return protocol in self.accepts


def _specs() -> dict[str, AgentSpec]:
    dsh_bin = os.environ.get(
        "NODUS_DSH_BIN",
        str(Path(r"A:\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js")),
    )
    return {
        "claude": AgentSpec(
            name="claude",
            vendor="claude",
            command=tuple(CLAUDE_ARGS),
            accepts=(providers.ANTHROPIC,),
            resume_mode="flag",
            fork_mode="flag",
        ),
        "codex": AgentSpec(
            name="codex",
            vendor="codex",
            command=(
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
            ),
            accepts=(providers.OPENAI,),
            resume_mode="subcommand",
            fork_mode="subcommand",
        ),
        "dsh": AgentSpec(
            name="dsh",
            vendor="dsh",
            command=(
                os.environ.get("NODUS_NODE", "node"),
                dsh_bin,
                "headless",
                "--json",
                "-",
            ),
            accepts=(providers.DEEPSEEK,),
            resume_mode="flag",
            fork_mode="",  # no native fork; `branch` seeds a new session instead
            # DSH keeps its sessions in a location the gateway already tails.
            # Claude Code runs from an isolated config dir it does not scan.
            gateway_sessions=True,
        ),
        # One spec per model rather than one per CLI: opencode has no per-run
        # default worth trusting, and the spec name is what a task asks for.
        **{
            name: AgentSpec(
                name=name,
                vendor="opencode",
                command=OPENCODE_ARGS,
                # opencode can be pointed at any OpenAI-compatible endpoint,
                # which is what its config declares (`@ai-sdk/openai-compatible`
                # against the relay's /v1). Nothing here injects that endpoint:
                # `agents.build_env` still materialises OPENAI_BASE_URL/API_KEY
                # for the provider, and opencode ignores both in favour of its
                # own `~/.config/opencode/opencode.json`.
                accepts=(providers.OPENAI,),
                resume_mode="flag",
                fork_mode="flag",
            )
            for name in ("opencode-glm", "opencode-gemini")
        },
    }


SPECS = _specs()


def registry() -> dict[str, AgentSpec]:
    """Pure lookup. Bootstrapping belongs to `profiles.materialize`."""
    return SPECS


def get(name: str) -> AgentSpec | None:
    return SPECS.get(name)


def unavailable(spec: AgentSpec) -> list[str]:
    """Reasons this CLI cannot run at all, independent of any endpoint."""
    out: list[str] = []
    exe = spec.command[0]
    if spec.vendor == "dsh":
        if not shutil.which(exe):
            out.append(f"command not found: {exe}")
        elif not Path(spec.command[1]).is_file():
            out.append(f"dsh entrypoint not found: {spec.command[1]}")
        return out
    if not shutil.which(exe):
        out.append(f"command not found: {exe}")
    return out


def missing(spec: AgentSpec, provider: providers.Provider, secrets: dict[str, str]) -> list[str]:
    out = unavailable(spec)
    out.extend(providers.missing(provider, secrets))
    out.extend(name for name in spec.secrets if not secrets.get(name))
    if not spec.accepts_protocol(provider.protocol):
        out.append(f"{spec.name} cannot speak {provider.protocol}")
    return out


def build_env(
    spec: AgentSpec,
    provider: providers.Provider,
    secrets: dict[str, str],
    home: Path | None = None,
) -> dict[str, str]:
    """Process environment for one run.

    Least privilege: the provider's own credential plus whatever the agent
    declares it needs — not every secret on the machine. Before the split there
    was no way to answer "which keys does this agent need", so everything was
    injected and every worker could read every key.
    """
    env = os.environ.copy()
    for name in spec.secrets:
        value = secrets.get(name)
        if value:
            env[name] = value
    env.update(providers.env(provider, secrets))
    if "ANTHROPIC_AUTH_TOKEN" in env:
        # Claude Code prefers x-api-key when an Anthropic key is inherited;
        # that would quietly outrank the token the provider just supplied.
        env.pop("ANTHROPIC_API_KEY", None)
    if home is not None:
        if spec.vendor == "claude":
            env["CLAUDE_CONFIG_DIR"] = str(home)
        elif spec.vendor == "codex":
            env["CODEX_HOME"] = str(home)
    env["NODUS_RUN"] = "1"
    return env


def argv(
    spec: AgentSpec,
    provider: providers.Provider,
    effort: str | None = None,
    session_id: str = "",
    model: str | None = None,
    session_mode: str = "resume",
    prompt: str = "",
) -> list[str]:
    """Build the command line for one attempt.

    The model is pinned on the command line rather than left to a config file,
    so the run record names what actually ran instead of what was configured.

    `prompt` is only used by a CLI that takes its message positionally; the
    others read it from stdin and would see a stray argument.
    """
    cmd = list(spec.command)
    chosen = model or provider.model
    if chosen:
        if spec.vendor == "codex":
            cmd += ["-c", f'model="{chosen}"']
        elif spec.vendor == "claude":
            cmd += ["--model", chosen]
        elif spec.vendor == "opencode":
            # `-m` is `provider/model` in opencode's own namespace. The relay's
            # OpenAI face names `gpt-6.1-sol`, which is not one of them, so only
            # a qualified name is passed on rather than an invalid flag.
            if "/" in chosen:
                cmd += ["-m", chosen]
        # dsh has no per-run model flag; the run record still keeps the intent.
    if effort:
        if spec.vendor == "claude":
            cmd += ["--effort", effort]
        elif spec.vendor == "codex":
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        # dsh and opencode keep effort in their own config (`--variant` for
        # opencode is model-specific and would fail on a model that has none).
    if session_id:
        cmd = _with_session(cmd, spec, session_id, session_mode)
    if prompt and spec.vendor == "opencode":
        # `opencode run "<message>"`: positional and last, because the CLI
        # collects every trailing argument into `message` before parsing flags.
        cmd = [*cmd, prompt]
    exe = shutil.which(cmd[0])
    if not exe:
        return cmd
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe, *cmd[1:]]
    return [exe, *cmd[1:]]


def _with_session(cmd: list[str], spec: AgentSpec, session_id: str, mode: str) -> list[str]:
    """Splice the vendor's own continue/branch syntax into an argv.

    Four shapes, and they are not interchangeable: a flag, a subcommand, a flag
    that has to sit before the stdin marker, and a flag pair before a
    positional prompt.
    """
    if mode == "fork" and not spec.fork_mode:
        raise ValueError(f"{spec.vendor} has no native fork; seed a new session instead")
    if not spec.resume_mode:
        raise ValueError(f"{spec.vendor} cannot continue a session")
    if spec.vendor == "dsh":
        # dsh reads the prompt from "-", so the flag must come before it
        return cmd[:-1] + ["--session-id", session_id, cmd[-1]]
    if spec.vendor == "claude":
        out = [*cmd, "--resume", session_id]
        if mode == "fork":
            out.append("--fork-session")
        return out
    if spec.vendor == "codex":
        verb = "fork" if mode == "fork" else "resume"
        # `codex exec resume <id> [flags]` — the subcommand sits right after exec
        return [*cmd[:2], verb, session_id, *cmd[2:]]
    if spec.vendor == "opencode":
        # `opencode run -s <id> [--fork] "<prompt>"`; the prompt is appended
        # after this, so the flags stay ahead of the positional message.
        out = [*cmd, "-s", session_id]
        if mode == "fork":
            out.append("--fork")
        return out
    raise ValueError(f"{spec.vendor} cannot continue a session")
