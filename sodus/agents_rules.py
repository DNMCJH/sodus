"""Install/uninstall sodus decision-check rules into AI host AGENTS.md files."""
from __future__ import annotations

from pathlib import Path
import re

RULE_TEXT = """- **新会话首轮**：先查未决决策：`sodus handoff decisions --all --json`；`count > 0` 时主动列出编号、项目、标题和推荐，请用户优先处理或明确跳过，再继续当前任务。"""

START_MARKER = "<!-- sodus-decisions:start -->"
END_MARKER = "<!-- sodus-decisions:end -->"

HOSTS = {
    "claude": Path.home() / ".claude" / "CLAUDE.md",
    "codex": Path.home() / ".codex" / "AGENTS.md",
    "dsh": Path("A:/dsh/home/AGENTS.md"),
}


def _find_sodus_section(content: str) -> tuple[int, int] | None:
    """Find the sodus section bounds (start line, end line) or None."""
    lines = content.splitlines(keepends=True)
    sodus_start = None
    for i, line in enumerate(lines):
        if "sodus" in line.lower() and line.strip().startswith("##"):
            sodus_start = i
            break
    if sodus_start is None:
        return None
    # Find end of section (next ## or EOF)
    for i in range(sodus_start + 1, len(lines)):
        if lines[i].strip().startswith("##"):
            return (sodus_start, i)
    return (sodus_start, len(lines))


def _inject_rule(content: str) -> str:
    """Inject or update the decision-check rule into sodus section."""
    lines = content.splitlines(keepends=True)

    # Remove existing rule block if present
    new_lines = []
    skip = False
    for line in lines:
        if START_MARKER in line:
            skip = True
            continue
        if END_MARKER in line:
            skip = False
            continue
        if not skip:
            new_lines.append(line)

    lines = new_lines
    bounds = _find_sodus_section("".join(lines))
    if not bounds:
        # No sodus section, append at end
        rule_block = f"\n{START_MARKER}\n{RULE_TEXT}\n{END_MARKER}\n"
        return "".join(lines) + rule_block

    start, end = bounds
    # Insert before the section's last line (usually a blank line or next section)
    insert_pos = end
    rule_block = f"{START_MARKER}\n{RULE_TEXT}\n{END_MARKER}\n"
    lines.insert(insert_pos, rule_block)

    return "".join(lines)


def _remove_rule(content: str) -> str:
    """Remove the decision-check rule block."""
    lines = content.splitlines(keepends=True)
    new_lines = []
    skip = False
    for line in lines:
        if START_MARKER in line:
            skip = True
            continue
        if END_MARKER in line:
            skip = False
            continue
        if not skip:
            new_lines.append(line)
    return "".join(new_lines)


def install(host: str | None = None, uninstall: bool = False) -> dict:
    """Install or uninstall decision-check rules.

    Args:
        host: "claude", "codex", "dsh", or None for all
        uninstall: True to remove rules instead of installing

    Returns:
        {"installed": [...], "failed": [...]}
    """
    targets = [host] if host else list(HOSTS.keys())
    installed = []
    failed = []

    for name in targets:
        path = HOSTS.get(name)
        if not path:
            failed.append({"host": name, "reason": "unknown host"})
            continue

        if not path.exists():
            failed.append({"host": name, "path": str(path), "reason": "file not found"})
            continue

        try:
            content = path.read_text(encoding="utf-8")
            if uninstall:
                new_content = _remove_rule(content)
            else:
                new_content = _inject_rule(content)

            path.write_text(new_content, encoding="utf-8")
            installed.append({"host": name, "path": str(path), "action": "uninstalled" if uninstall else "installed"})
        except Exception as e:
            failed.append({"host": name, "path": str(path), "reason": str(e)})

    return {"installed": installed, "failed": failed}


def check() -> dict:
    """Check installation status of decision-check rules.

    Returns:
        {"hosts": [{"host": str, "path": str, "installed": bool, "exists": bool}]}
    """
    result = []
    for name, path in HOSTS.items():
        exists = path.exists()
        installed = False
        if exists:
            try:
                content = path.read_text(encoding="utf-8")
                installed = START_MARKER in content and END_MARKER in content
            except Exception:
                pass
        result.append({"host": name, "path": str(path), "exists": exists, "installed": installed})
    return {"hosts": result}
