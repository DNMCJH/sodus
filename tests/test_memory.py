"""Memory: three scopes, keyword recall, and what lands in a brief.

The dispatch tests stub `Runner._attempt` / `Runner._finalize`: the assertion is
about the prompt.md the runner writes, not about anything a model would do with
it. No CLI, no network.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from nodus import agents, config, db, mcp, memory, providers, routes, runner
from nodus.cli import build_parser


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A state directory and workspace per test."""
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-a")
    return tmp_path


# ---- scope isolation ---------------------------------------------------


def test_private_memory_is_readable_by_its_owner_only(home):
    memory.remember(
        "部署密钥放在 1Password 的 nodus 条目",
        scope="private",
        key="deploy-key",
        agent="claude",
    )

    assert [row["key"] for row in memory.recall("部署密钥", agent="claude")] == ["deploy-key"]
    # Another agent, the anonymous caller, and an explicit private query from
    # somebody else all see nothing.
    assert memory.recall("部署密钥", agent="codex") == []
    assert memory.recall("部署密钥") == []
    assert memory.recall("部署密钥", scope="private", agent="codex") == []
    assert memory.context_for("部署密钥在哪", agent="codex") == ""


def test_private_memory_needs_an_owner(home):
    with pytest.raises(ValueError):
        memory.remember("没有归属的秘密", scope="private")


def test_project_memory_is_shared_inside_one_workspace_only(home, monkeypatch):
    memory.remember("构建脚本统一用 pnpm", scope="project", key="build")
    assert [row["key"] for row in memory.recall("构建")] == ["build"]

    monkeypatch.setenv("NODUS_WORKSPACE", "ws-b")
    assert memory.recall("构建") == []
    assert memory.recall("构建", scope="project") == []

    monkeypatch.setenv("NODUS_WORKSPACE", "ws-a")
    assert [row["key"] for row in memory.recall("构建")] == ["build"]


def test_public_memory_is_visible_from_every_workspace(home, monkeypatch):
    memory.remember("提交信息用中文", scope="public", key="commit")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-anywhere-else")
    assert [row["key"] for row in memory.recall("提交")] == ["commit"]


def test_private_memory_follows_the_agent_not_the_workspace(home, monkeypatch):
    memory.remember("我的调试端口是 9222", scope="private", key="port", agent="claude")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-b")
    assert [row["key"] for row in memory.recall("端口", agent="claude")] == ["port"]
    assert memory.recall("端口", agent="codex") == []


def test_forget_cannot_cross_the_private_boundary(home):
    memory.remember("秘密", scope="private", key="k", agent="claude")
    assert memory.forget("k", agent="codex") == 0
    assert memory.forget("k", agent="claude") == 1
    assert memory.recall("秘密", agent="claude") == []


# ---- overwrite ---------------------------------------------------------


def test_same_key_replaces_the_fact_and_bumps_updated_at(home):
    first = memory.remember("构建用 npm", scope="project", key="build")
    second = memory.remember("构建用 pnpm", scope="project", key="build")

    assert second["id"] == first["id"]
    assert second["text"] == "构建用 pnpm"
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] > first["updated_at"]
    rows = memory.recall("构建")
    assert len(rows) == 1
    assert rows[0]["text"] == "构建用 pnpm"


def test_the_same_key_in_another_scope_is_another_fact(home):
    project = memory.remember("项目内可见", scope="project", key="same")
    public = memory.remember("全局可见", scope="public", key="same")
    assert project["id"] != public["id"]
    assert {row["id"] for row in memory.recall("可见")} == {project["id"], public["id"]}


def test_tags_are_kept_and_searchable(home):
    row = memory.remember("发布前要跑 smoke", scope="project", key="release", tags=["release", "ci"])
    assert row["tags"] == ["release", "ci"]
    assert [item["key"] for item in memory.recall("ci")] == ["release"]


# ---- recall ordering ---------------------------------------------------


def test_recall_ranks_more_matched_terms_first(home):
    both = memory.remember("构建脚本换成 pnpm", scope="project", key="both")
    one = memory.remember("构建脚本要迁移", scope="project", key="one")

    rows = memory.recall("构建 pnpm")
    # `one` is newer, but `both` matches two query terms instead of one.
    assert [row["id"] for row in rows] == [both["id"], one["id"]]


def test_recall_without_a_query_is_newest_first(home):
    older = memory.remember("旧事实", scope="project")
    newer = memory.remember("新事实", scope="project")
    assert [row["id"] for row in memory.recall()] == [newer["id"], older["id"]]


def test_recall_never_returns_unrelated_rows_for_a_query(home):
    memory.remember("构建脚本统一用 pnpm", scope="project", key="build")
    assert memory.recall("配色方案") == []


# ---- context_for -------------------------------------------------------


def test_context_for_is_empty_when_nothing_is_remembered(home):
    assert memory.context_for("构建脚本") == ""


def test_context_for_labels_the_facts_it_returns(home):
    memory.remember("构建脚本统一用 pnpm", scope="project", key="build")
    block = memory.context_for("重构构建脚本")
    assert block.startswith("以下是这个项目已确认的事实：")
    assert "- [project/build] 构建脚本统一用 pnpm" in block


def test_context_for_skips_another_agents_private_facts(home):
    memory.remember("密钥在 1Password", scope="private", key="secret", agent="claude")
    memory.remember("构建脚本统一用 pnpm", scope="public", key="build")

    block = memory.context_for("构建脚本和密钥", agent="codex")
    assert "构建脚本统一用 pnpm" in block
    assert "1Password" not in block


# ---- dispatch injection -------------------------------------------------


def _stub_execution(monkeypatch):
    """Route/provider/agent plumbing plus the two calls that would spawn a CLI."""
    route = routes.Route(name="fake", agent="fake", providers=("p0",))
    spec = agents.AgentSpec(
        name="fake",
        vendor="claude",
        command=(sys.executable, "-c", "pass"),
        accepts=(providers.ANTHROPIC,),
    )
    provider = providers.Provider(name="p0", protocol=providers.ANTHROPIC)
    monkeypatch.setattr(routes, "resolve_for_run", lambda name: route)
    monkeypatch.setattr(routes, "problems", lambda value: [])
    monkeypatch.setattr(providers, "load", lambda: {"p0": provider})
    monkeypatch.setattr(agents, "get", lambda name: spec)
    monkeypatch.setattr(agents, "missing", lambda spec, value, secrets: [])
    monkeypatch.setattr(runner.pet, "narrate", lambda *args, **kwargs: None)

    def fake_attempt(
        self, rid, index, spec, provider, workdir, secrets, profile_name,
        prompt, effort, model, session_id, session_mode,
    ):
        stamp = time.time()
        return {
            "index": index,
            "provider": provider.name,
            "model": model or "",
            "started_at": stamp,
            "ended_at": stamp,
            "exit_code": 0,
            "error": "",
            "errors": [],
            "ok": True,
            "tool_calls": 0,
            "output_lines": 1,
            "noise": [],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read": 0,
                "cache_write": 0,
            },
            "texts": ["ok"],
            "session_ref": {},
        }

    def fake_finalize(self, rid, *args, **kwargs):
        db.update_run(rid, status="completed", ended_at=time.time())

    monkeypatch.setattr(runner.Runner, "_attempt", fake_attempt)
    monkeypatch.setattr(runner.Runner, "_finalize", fake_finalize)


def _prompt_of(tmp_path, monkeypatch, include_memory: bool = True) -> str:
    _stub_execution(monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    task = db.create_task("把构建脚本迁到 pnpm", acceptance="pytest green")
    memory.remember("构建脚本统一用 pnpm", scope="project", key="build")
    run = runner.RUNNER.start(
        task["id"], "fake", cwd=str(work), include_memory=include_memory
    )
    finished = runner.RUNNER.wait(run["id"], timeout=10)
    assert finished["status"] == "completed"
    return (config.HOME / "runs" / run["id"] / "prompt.md").read_text(encoding="utf-8")


def test_start_injects_remembered_facts_into_the_brief(home, tmp_path, monkeypatch):
    prompt = _prompt_of(tmp_path, monkeypatch)
    assert "以下是这个项目已确认的事实：" in prompt
    assert "- [project/build] 构建脚本统一用 pnpm" in prompt


def test_start_can_switch_memory_off(home, tmp_path, monkeypatch):
    prompt = _prompt_of(tmp_path, monkeypatch, include_memory=False)
    assert "以下是这个项目已确认的事实" not in prompt
    assert "构建脚本统一用 pnpm" not in prompt


def test_run_exposes_the_no_memory_flag():
    parser = build_parser()
    assert parser.parse_args(["run", "task-x", "--no-memory"]).no_memory is True
    assert parser.parse_args(["run", "task-x"]).no_memory is False


# ---- MCP surface -------------------------------------------------------


def _payload(result: dict) -> dict:
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def test_mcp_exposes_the_memory_tools(home):
    names = {tool["name"] for tool in mcp.TOOLS}
    assert {"nodus_memory_remember", "nodus_memory_recall"} <= names


def test_mcp_memory_tools_keep_private_rows_private(home):
    saved = _payload(
        mcp.call_tool(
            "nodus_memory_remember",
            {"text": "密钥在 1Password", "scope": "private", "key": "k", "agent": "claude"},
        )
    )
    assert saved["key"] == "k"

    mine = _payload(
        mcp.call_tool("nodus_memory_recall", {"query": "密钥", "agent": "claude"})
    )
    assert mine["count"] == 1

    blind = _payload(
        mcp.call_tool("nodus_memory_recall", {"query": "密钥", "agent": "codex"})
    )
    assert blind["count"] == 0

    denied = mcp.call_tool("nodus_memory_remember", {"text": "x", "scope": "private"})
    assert denied["isError"] is True
