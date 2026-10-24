"""Memory: harvest the harnesses' own files, dedupe, flag conflicts, adopt.

Nothing here touches a real memory file: `_user_home` is pointed at a temp
directory and `DSH_HOME` is cleared, so the assertions are about the rules and
not about whatever this machine happens to have written down. The dispatch test
stubs `Runner._attempt` / `Runner._finalize`: the assertion is about the
prompt.md the runner writes, not about anything a model would do with it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from nodus import agents, config, db, mcp, memory, providers, routes, runner
from nodus.cli import build_parser
TODAY = date.today().isoformat()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A fake home and a fake project: no real memory file is ever read."""
    home = tmp_path / "home"
    project = tmp_path / "proj"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(config, "HOME", tmp_path / "nodus")
    monkeypatch.setattr(memory, "_user_home", lambda: home)
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.delenv("NODUS_PROJECT", raising=False)
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-mem")
    return SimpleNamespace(home=home, project=project, tmp=tmp_path)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def facts(project_dir: Path) -> dict[str, dict]:
    return {row["text"]: row for row in memory.harvest(project_dir)}


# ---- sources -----------------------------------------------------------


def test_sources_reports_scope_owner_and_size(env):
    write(env.project / "AGENTS.md", "# P\n\n- 构建用 pnpm\n")
    write(env.project / "CLAUDE.md", "@AGENTS.md\n")
    write(env.project / "sub" / "AGENTS.md", "- 子目录里的一条\n")
    write(env.home / ".codex" / "AGENTS.md", "- 全局一条\n")
    write(env.home / ".claude" / "CLAUDE.md", "- 全局二条\n")
    write(env.home / ".nodus" / "AGENTS.md", "- 第三条\n")

    rows = {
        memory.short_path(row["path"], env.project): (
            row["scope"],
            row["harness"],
            row["lines"],
        )
        for row in memory.sources(env.project)
    }
    assert rows == {
        "AGENTS.md": ("project", "other", 3),
        "CLAUDE.md": ("project", "claude", 1),
        "sub/AGENTS.md": ("project", "other", 1),
        "~/.codex/AGENTS.md": ("global", "codex", 1),
        "~/.claude/CLAUDE.md": ("global", "claude", 1),
        "~/.nodus/AGENTS.md": ("global", "other", 1),
    }


def test_sources_only_looks_one_level_down_and_skips_debris(env):
    write(env.project / "AGENTS.md", "- 顶层\n")
    write(env.project / "nodus" / "AGENTS.md", "- 一层\n")
    write(env.project / "nodus" / "deep" / "AGENTS.md", "- 两层，不该读到\n")
    write(env.project / "node_modules" / "AGENTS.md", "- 依赖目录，不该读到\n")
    write(env.project / ".hidden" / "AGENTS.md", "- 隐藏目录，不该读到\n")

    paths = {memory.short_path(row["path"], env.project) for row in memory.sources(env.project)}
    assert paths == {"AGENTS.md", "nodus/AGENTS.md"}


def test_a_missing_memory_file_is_not_reported(env):
    assert memory.sources(env.project) == []


def test_dsh_home_is_a_source_when_it_is_set(env, monkeypatch):
    dsh = env.tmp / "dsh-home"
    write(dsh / "AGENTS.md", "- 全局指令\n")
    monkeypatch.setenv("DSH_HOME", str(dsh))
    rows = memory.sources(env.project)
    assert [(row["scope"], row["harness"]) for row in rows] == [("global", "dsh")]


# ---- extraction --------------------------------------------------------


def test_harvest_reads_instructions_not_headings(env):
    write(
        env.project / "AGENTS.md",
        "# 标题不算\n\n## 开工顺序\n\n- 读 `INDEX.md` 拿路径\n1. 先跑 pytest\n"
        "构建统一用 pnpm，不要用 npm。\n这句是普通描述，抽不出来。\n",
    )
    rows = memory.harvest(env.project)
    kinds = {row["text"]: row["kind"] for row in rows}
    # a heading is an outline label; adopting it would copy the outline
    assert "开工顺序" not in kinds
    assert kinds["读 INDEX.md 拿路径"] == "bullet"
    assert kinds["先跑 pytest"] == "bullet"
    # the sentence is split on 。 and only the imperative half is kept
    assert "构建统一用 pnpm，不要用 npm。" in kinds
    assert "这句是普通描述，抽不出来" not in kinds
    # a level-1 heading is a document title, not a fact
    assert "标题不算" not in kinds


def test_descriptions_values_and_headings_are_not_candidates(env):
    # "这里只回答一个问题…" is what the board was full of: prose about the
    # document itself, which nobody can adopt. `port: 30874` is a value.
    write(
        env.project / "AGENTS.md",
        "# P\n\n## 边界\n\n这里只回答一个问题：下次开工该知道什么。\n"
        "- 端口：30874\n- Main workspace: A:\\VScode\\Code\\Projects\n- 先跑 pytest\n",
    )
    assert [row["text"] for row in memory.harvest(env.project)] == ["先跑 pytest"]


def test_harvest_skips_checkbox_templates_and_placeholders(env):
    write(
        env.project / "AGENTS.md",
        "- [ ] **[file:line]** 待办\n- 示例见 <path/to/file>\n- 正常的一条事实\n",
    )
    assert [row["text"] for row in memory.harvest(env.project)] == ["正常的一条事实"]


def test_a_bare_path_bullet_is_not_a_fact(env):
    write(env.project / "AGENTS.md", "- scripts/clean_merge.py\n- 配置在 scripts/ 下\n")
    assert [row["text"] for row in memory.harvest(env.project)] == ["配置在 scripts/ 下"]


# ---- dedupe ------------------------------------------------------------


def test_the_same_fact_in_two_harnesses_is_one_candidate(env):
    write(env.project / "AGENTS.md", "- 构建脚本统一用 pnpm\n")
    write(env.home / ".codex" / "AGENTS.md", "- 构建脚本统一用 pnpm\n")
    rows = memory.harvest(env.project)
    assert len(rows) == 1
    assert [source["harness"] for source in rows[0]["sources"]] == ["other", "codex"]
    assert {memory.short_path(s["path"], env.project) for s in rows[0]["sources"]} == {
        "AGENTS.md",
        "~/.codex/AGENTS.md",
    }


def test_near_duplicates_merge_and_keep_both_sources(env):
    write(env.project / "AGENTS.md", "- 构建脚本统一用 pnpm\n")
    write(env.home / ".codex" / "AGENTS.md", "- 构建脚本一律用 pnpm\n")
    rows = memory.harvest(env.project)
    assert len(rows) == 1
    assert len(rows[0]["sources"]) == 2


def test_two_different_facts_stay_two_candidates(env):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n- 发布前跑 smoke\n")
    assert len(memory.harvest(env.project)) == 2


# ---- conflicts ---------------------------------------------------------


def test_two_package_managers_are_flagged_as_a_conflict(env):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n")
    write(env.home / ".codex" / "AGENTS.md", "- 构建用 npm\n")
    rows = memory.harvest(env.project)
    groups = memory.conflicts(rows)
    assert len(rows) == 2
    assert len(groups) == 1
    assert set(groups[0]) == {row["id"] for row in rows}
    assert all(row["conflict"] for row in rows)


def test_do_it_and_do_not_do_it_conflict(env):
    write(env.project / "AGENTS.md", "- 用 npm 装依赖\n")
    write(env.home / ".codex" / "AGENTS.md", "- 不要用 npm 装依赖\n")
    rows = memory.harvest(env.project)
    assert len(rows) == 2
    assert len(memory.conflicts(rows)) == 1
    assert all(row["conflict"] for row in rows)


def test_forbidding_one_tool_and_picking_its_rival_is_not_a_conflict(env):
    # "don't use npm" and "use pnpm" say the same thing; flagging them would be
    # the kind of noise that teaches the reviewer to ignore the flag.
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n")
    write(env.home / ".codex" / "AGENTS.md", "- 不要用 npm 装依赖\n")
    rows = memory.harvest(env.project)
    assert memory.conflicts(rows) == []


def test_agreeing_facts_are_not_a_conflict(env):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n- 提交前跑 pnpm test\n")
    rows = memory.harvest(env.project)
    assert memory.conflicts(rows) == []
    assert not any(row["conflict"] for row in rows)


def test_a_negation_elsewhere_in_the_line_is_not_a_conflict(env):
    # Both mention grep; only one is negated, and the negation is about the
    # reader's conclusion rather than about using grep.
    write(
        env.project / "AGENTS.md",
        "- 没有挂载外部目录，grep 只扫工作区，别误判成文件不存在\n"
        "- After sync, run remote grep key logic.\n",
    )
    rows = memory.harvest(env.project)
    assert memory.conflicts(rows) == []


# ---- adopt -------------------------------------------------------------


def _only(project_dir: Path, text: str) -> dict:
    return next(row for row in memory.harvest(project_dir) if row["text"] == text)


def test_adopt_appends_the_fact_with_date_and_source(env):
    write(env.project / "AGENTS.md", "# 项目\n\n## 边界\n\n- 不复制源码进来\n")
    write(env.home / ".codex" / "AGENTS.md", "- 构建用 pnpm\n")
    result = memory.adopt([_only(env.project, "构建用 pnpm")["id"]], env.project)

    assert result["added"] == 1
    target = env.home / ".nodus" / "AGENTS.md"
    assert result["path"] == str(target)
    text = target.read_text(encoding="utf-8")
    assert "## 已验证事实" in text
    assert f"- {TODAY}（来源 ~/.codex/AGENTS.md）构建用 pnpm" in text
    # a global preference never lands in a project's file
    project_text = (env.project / "AGENTS.md").read_text(encoding="utf-8")
    assert project_text == "# 项目\n\n## 边界\n\n- 不复制源码进来\n"
    # the human's own section is untouched
    assert "## 边界\n\n- 不复制源码进来" in project_text


def test_a_project_fact_goes_to_the_project_file_only(env):
    write(env.project / "sub" / "AGENTS.md", "- 提交信息用中文\n")
    result = memory.adopt([_only(env.project, "提交信息用中文")["id"]], env.project)

    assert result["path"] == str(env.project / "AGENTS.md")
    text = (env.project / "AGENTS.md").read_text(encoding="utf-8")
    assert f"- {TODAY}（来源 sub/AGENTS.md）提交信息用中文" in text
    assert not (env.home / ".nodus" / "AGENTS.md").exists()


def test_adopt_names_every_source_of_a_merged_fact(env):
    write(env.project / "AGENTS.md", "- 构建脚本统一用 pnpm\n")
    write(env.home / ".codex" / "AGENTS.md", "- 构建脚本统一用 pnpm\n")
    memory.adopt([_only(env.project, "构建脚本统一用 pnpm")["id"]], env.project)
    text = (env.project / "AGENTS.md").read_text(encoding="utf-8")
    assert f"- {TODAY}（来源 AGENTS.md、~/.codex/AGENTS.md）构建脚本统一用 pnpm" in text


def test_adopting_twice_does_not_write_twice(env):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n")
    cand = _only(env.project, "构建用 pnpm")
    memory.adopt([cand["id"]], env.project)
    after_first = (env.project / "AGENTS.md").read_text(encoding="utf-8")

    second = memory.adopt([cand["id"]], env.project)
    assert second["added"] == 0
    assert second["already"] == 1
    assert (env.project / "AGENTS.md").read_text(encoding="utf-8") == after_first


def test_adopt_creates_the_global_file_for_a_global_fact(env):
    write(env.home / ".codex" / "AGENTS.md", "- 提交信息用中文\n")
    memory.adopt([_only(env.project, "提交信息用中文")["id"]], env.project)
    text = (env.home / ".nodus" / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("## 已验证事实")
    assert "提交信息用中文" in text
    assert not (env.project / "AGENTS.md").exists()


def test_adopt_refuses_an_unknown_id(env):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n")
    with pytest.raises(ValueError):
        memory.adopt(["mem-0000000000"], env.project)


def test_harvest_marks_what_is_already_adopted(env):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n")
    assert not _only(env.project, "构建用 pnpm")["adopted"]
    memory.adopt([_only(env.project, "构建用 pnpm")["id"]], env.project)
    assert _only(env.project, "构建用 pnpm")["adopted"] is True


def test_the_line_adopt_wrote_does_not_come_back_as_a_new_candidate(env):
    write(env.home / ".codex" / "AGENTS.md", "- 构建用 pnpm\n")
    memory.adopt([_only(env.project, "构建用 pnpm")["id"]], env.project)
    rows = memory.harvest(env.project)
    assert [row["text"] for row in rows] == ["构建用 pnpm"]
    assert rows[0]["adopted"] is True
    # the written line is skipped, not re-read as a second source
    assert len(rows[0]["sources"]) == 1
    assert not (env.project / "AGENTS.md").exists()


def test_a_fact_line_is_skipped_even_without_its_source(env):
    # The marker alone is enough: `（来源 …）` is what `adopt` writes, and
    # reading it back is how an adopted fact became a candidate again.
    write(
        env.project / "AGENTS.md",
        f"## 已验证事实\n\n- {TODAY}（来源 AGENTS.md）构建用 pnpm\n\n"
        "## 边界\n\n- 先跑 pytest\n",
    )
    assert [row["text"] for row in memory.harvest(env.project)] == ["先跑 pytest"]


def test_a_second_click_on_an_adopted_id_says_already(env):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n")
    cand = _only(env.project, "构建用 pnpm")
    first = memory.adopt([cand["id"]], env.project)
    second = memory.adopt([cand["id"]], env.project)
    assert (first["added"], first["already"]) == (1, 0)
    assert (second["added"], second["already"], second["ok"]) == (0, 1, True)


# ---- the board budget ---------------------------------------------------


def test_the_source_budget_keeps_the_rows_that_read_like_orders(env, monkeypatch):
    monkeypatch.setattr(memory, "MAX_PER_SOURCE", 1)
    monkeypatch.setattr(memory, "MAX_BOARD", 10)
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n- 先跑 pytest\n")
    assert [row["text"] for row in memory.harvest(env.project)] == ["先跑 pytest"]


def test_the_board_budget_spans_every_source(env, monkeypatch):
    monkeypatch.setattr(memory, "MAX_BOARD", 2)
    write(env.project / "AGENTS.md", "- 先跑 pytest\n- 提交前跑 pytest -q\n")
    write(env.home / ".codex" / "AGENTS.md", "- 构建用 pnpm\n- 发布前打 tag\n")
    assert len(memory.harvest(env.project)) == 2


# ---- verified_facts / remember ----------------------------------------


def test_verified_facts_returns_the_section(env):
    write(
        env.project / "AGENTS.md",
        "# P\n\n## 已验证事实\n\n"
        f"- {TODAY}（来源 AGENTS.md）构建用 pnpm\n\n## 边界\n\n- 别的\n",
    )
    block = memory.verified_facts(env.project)
    assert block.startswith("## 已验证事实")
    assert "构建用 pnpm" in block
    assert "别的" not in block


def test_verified_facts_is_empty_without_the_section(env):
    write(env.project / "AGENTS.md", "# P\n\n- 没有小节\n")
    assert memory.verified_facts(env.project) == ""
    assert memory.verified_facts(env.tmp / "nowhere") == ""


def test_remember_writes_one_line_and_does_not_duplicate(env):
    first = memory.remember("构建用 pnpm", env.project, source="agent")
    assert first["added"] == 1
    again = memory.remember("构建用 pnpm", env.project, source="agent")
    assert again["added"] == 0
    text = (env.project / "AGENTS.md").read_text(encoding="utf-8")
    assert text.count("构建用 pnpm") == 1
    assert f"- {TODAY}（来源 agent）构建用 pnpm" in text


def test_remember_needs_text(env):
    with pytest.raises(ValueError):
        memory.remember("   ", env.project)


# ---- the HTTP surface ---------------------------------------------------


@pytest.fixture()
def board_server(env, monkeypatch):
    """A real Handler on an ephemeral port, so auth and routing are covered."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from nodus import server

    monkeypatch.setenv("NODUS_PROJECT", str(env.project))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def post(path: str, payload: dict, token: str = "") -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            base + path, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def get(path: str, token: str = "") -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = urllib.request.Request(base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    post.get = get
    try:
        yield post
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


def test_http_adopt_requires_the_token(env, board_server):
    write(env.home / ".codex" / "AGENTS.md", "- 构建用 pnpm\n")
    cand = _only(env.project, "构建用 pnpm")
    status, body = board_server("/memory/adopt", {"ids": [cand["id"]]})
    assert status == 401
    assert not (env.project / "AGENTS.md").exists()


def test_http_adopt_writes_the_fact_and_is_idempotent(env, board_server):
    write(env.home / ".codex" / "AGENTS.md", "- 构建用 pnpm\n")
    cand = _only(env.project, "构建用 pnpm")
    token = config.token()

    status, body = board_server("/memory/adopt", {"ids": [cand["id"]]}, token=token)
    assert (status, body["added"]) == (200, 1)
    target = env.home / ".nodus" / "AGENTS.md"
    assert body["path"] == str(target)
    assert "构建用 pnpm" in target.read_text(encoding="utf-8")
    # the project file is not created for a global fact
    assert not (env.project / "AGENTS.md").exists()

    status, body = board_server("/memory/adopt", {"ids": [cand["id"]]}, token=token)
    assert (status, body["added"], body["already"]) == (200, 0, 1)


def test_http_adopt_rejects_a_bad_request(env, board_server):
    token = config.token()
    assert board_server("/memory/adopt", {"ids": []}, token=token)[0] == 400
    assert board_server("/memory/adopt", {"ids": ["nope"]}, token=token)[0] == 400
    assert board_server("/memory/adopt", {"ids": ["mem-0000000000"]}, token=token)[0] == 400


def test_http_adopt_reports_the_actual_error(env, board_server, monkeypatch):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n")
    cand = _only(env.project, "构建用 pnpm")

    def broken(ids, project_dir=None):
        raise RuntimeError("disk is on fire")

    monkeypatch.setattr(memory, "adopt", broken)
    status, body = board_server(
        "/memory/adopt", {"ids": [cand["id"]]}, token=config.token()
    )
    assert status == 500
    assert "disk is on fire" in body["error"]


def test_http_adopt_reports_a_write_failure(env, board_server, monkeypatch):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n")
    cand = _only(env.project, "构建用 pnpm")

    def readonly(ids, project_dir=None):
        raise OSError("read-only file system")

    monkeypatch.setattr(memory, "adopt", readonly)
    status, body = board_server(
        "/memory/adopt", {"ids": [cand["id"]]}, token=config.token()
    )
    assert status == 409
    assert "read-only file system" in body["error"]


def test_the_board_lists_only_pending_candidates(env, board_server):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n- 先跑 pytest\n")
    memory.adopt([_only(env.project, "构建用 pnpm")["id"]], env.project)

    status, body = board_server.get("/board", config.token())
    assert status == 200
    texts = [row["text"] for row in body["memory_candidates"]]
    assert "构建用 pnpm" not in texts
    assert "先跑 pytest" in texts



# ---- which project -----------------------------------------------------


def test_project_dir_is_the_cwd_unless_the_environment_says_otherwise(env, monkeypatch):
    monkeypatch.chdir(env.project)
    assert memory.project_root() == str(env.project.resolve())

    other = env.tmp / "other"
    other.mkdir()
    monkeypatch.setenv("NODUS_PROJECT", str(other))
    assert memory.project_root() == str(other.resolve())


def test_the_board_follows_the_project_the_cli_recorded(env, monkeypatch):
    # The daemon runs in the state directory, so its cwd is not a project. The
    # last `debug memory` run leaves a pointer in config.json; the env wins.
    monkeypatch.chdir(env.tmp)
    config.ensure_dirs()
    config.config_path().write_text(
        json.dumps({"project_dir": str(env.project)}), encoding="utf-8"
    )
    assert memory.saved_project() == env.project.resolve()
    # A harvest from somewhere else still means "somewhere else".
    assert memory.project_root() == str(Path.cwd().resolve())

    other = env.tmp / "other"
    other.mkdir()
    monkeypatch.setenv("NODUS_PROJECT", str(other))
    assert memory.saved_project() == other.resolve()


# ---- the old SQLite store is gone --------------------------------------


def test_connect_drops_the_legacy_memory_table_and_keeps_its_rows(env):
    config.ensure_dirs()
    raw = sqlite3.connect(config.HOME / "nodus.sqlite3")
    raw.execute("CREATE TABLE memory (id TEXT PRIMARY KEY, text TEXT)")
    raw.execute("INSERT INTO memory VALUES('mem-0000000001','旧的一条事实')")
    raw.commit()
    raw.close()

    db.connect().close()

    raw = sqlite3.connect(config.HOME / "nodus.sqlite3")
    tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    raw.close()
    assert "memory" not in tables
    backup = json.loads((config.HOME / "memory-legacy.json").read_text(encoding="utf-8"))
    assert backup == [{"id": "mem-0000000001", "text": "旧的一条事实"}]


# ---- dispatch injection ------------------------------------------------


def _stub_execution(monkeypatch, reads_memory_files: bool):
    """Route/provider/agent plumbing plus the two calls that spawn a CLI."""
    route = routes.Route(name="fake", agent="fake", providers=("p0",))
    spec = agents.AgentSpec(
        name="fake",
        vendor="claude",
        command=(sys.executable, "-c", "pass"),
        accepts=(providers.ANTHROPIC,),
        reads_memory_files=reads_memory_files,
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


def _prompt_of(env, monkeypatch, reads_memory_files: bool, include_memory: bool = True) -> str:
    _stub_execution(monkeypatch, reads_memory_files)
    work = env.tmp / "work"
    write(
        work / "AGENTS.md",
        f"# P\n\n## 已验证事实\n\n- {TODAY}（来源 CLAUDE.md）构建用 pnpm\n",
    )
    task = db.create_task("把构建脚本迁到 pnpm", acceptance="pytest green")
    run = runner.RUNNER.start(
        task["id"], "fake", cwd=str(work), include_memory=include_memory
    )
    finished = runner.RUNNER.wait(run["id"], timeout=10)
    assert finished["status"] == "completed"
    return (config.HOME / "runs" / run["id"] / "prompt.md").read_text(encoding="utf-8")


def test_facts_are_injected_only_for_a_harness_that_cannot_read_files(env, monkeypatch):
    prompt = _prompt_of(env, monkeypatch, reads_memory_files=False)
    assert "## 已验证事实" in prompt
    assert "构建用 pnpm" in prompt


def test_a_file_reading_harness_gets_no_memory_block(env, monkeypatch):
    # Claude Code / Codex / DSH read AGENTS.md themselves; repeating it in the
    # brief is paid on every attempt and says nothing new.
    prompt = _prompt_of(env, monkeypatch, reads_memory_files=True)
    assert "## 已验证事实" not in prompt
    assert "构建用 pnpm" not in prompt


def test_the_injection_can_still_be_switched_off(env, monkeypatch):
    prompt = _prompt_of(env, monkeypatch, reads_memory_files=False, include_memory=False)
    assert "已验证事实" not in prompt


def test_run_exposes_the_no_memory_flag():
    parser = build_parser()
    assert parser.parse_args(["run", "task-x", "--no-memory"]).no_memory is True
    assert parser.parse_args(["run", "task-x"]).no_memory is False


def test_every_builtin_agent_reads_the_files_itself():
    # If a vendor is added that ignores AGENTS.md/CLAUDE.md, this is the line
    # that has to change -- together with a test that its brief carries them.
    assert all(spec.reads_memory_files for spec in agents.registry().values())


# ---- MCP surface -------------------------------------------------------


def _payload(result: dict) -> dict:
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def test_mcp_exposes_the_memory_tools(env):
    names = {tool["name"] for tool in mcp.TOOLS}
    assert {"nodus_memory_remember", "nodus_memory_candidates"} <= names
    assert "nodus_memory_recall" not in names


def test_mcp_remember_writes_into_agents_md(env, monkeypatch):
    monkeypatch.chdir(env.project)
    saved = _payload(mcp.call_tool("nodus_memory_remember", {"text": "构建用 pnpm"}))
    assert saved["added"] == 1
    assert (env.project / "AGENTS.md").is_file()
    again = _payload(mcp.call_tool("nodus_memory_remember", {"text": "构建用 pnpm"}))
    assert again["added"] == 0


def test_mcp_candidates_lists_sources_and_conflicts(env, monkeypatch):
    write(env.project / "AGENTS.md", "- 构建用 pnpm\n")
    write(env.home / ".codex" / "AGENTS.md", "- 构建用 npm\n")
    monkeypatch.chdir(env.project)

    everything = _payload(mcp.call_tool("nodus_memory_candidates", {}))
    assert everything["count"] == 2
    assert {row["text"] for row in everything["candidates"]} == {"构建用 pnpm", "构建用 npm"}
    assert all(row["sources"] for row in everything["candidates"])
    assert everything["project"] == str(env.project.resolve())

    conflicts = _payload(mcp.call_tool("nodus_memory_candidates", {"conflicts_only": True}))
    assert conflicts["count"] == 2
    assert all(row["conflict"] for row in conflicts["candidates"])

    limited = _payload(mcp.call_tool("nodus_memory_candidates", {"limit": 1}))
    assert len(limited["candidates"]) == 1


def test_mcp_memory_tools_reject_bad_arguments(env, monkeypatch):
    monkeypatch.chdir(env.project)
    assert mcp.call_tool("nodus_memory_remember", {"text": ""})["isError"] is True
    assert mcp.call_tool("nodus_memory_candidates", {"nope": 1})["isError"] is True
