"""Sessions across vendors: read any of the three, continue or branch any of them."""

from __future__ import annotations

import json

import pytest
import zstandard

from sodus import agents, config, db, providers, sessions
from sodus.sessions import claude, codex, dsh, model

NOW = 1_791_000_000_000


# ---- fixtures ----------------------------------------------------------


def make_dsh(root, project: str, sid: str, cwd: str, title: str = "标题") -> None:
    rows = [
        {"type": "session", "version": 4, "id": sid, "createdAt": NOW, "cwd": cwd},
        {"type": "session/title", "seq": 1, "time": NOW, "data": {"title": title}},
        {
            "type": "user/message",
            "seq": 2,
            "time": NOW + 1000,
            "data": {
                "content": [{"type": "text", "text": "看看 https://example.com/x"}],
                "source": {"kind": "user"},
            },
        },
        {
            "type": "user/message",
            "seq": 3,
            "time": NOW + 2000,
            "data": {
                "content": [{"type": "text", "text": "系统提醒噪音"}],
                "source": {"kind": "system-reminder"},
            },
        },
        {
            "type": "assistant/message",
            "seq": 4,
            "time": NOW + 3000,
            "data": {
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "内部推理"},
                        {"type": "text", "text": "好了。"},
                    ]
                }
            },
        },
    ]
    folder = root / project / sid
    folder.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    with (folder / dsh.FILENAME).open("wb") as f:
        with zstandard.ZstdCompressor().stream_writer(f) as writer:
            writer.write(payload.encode("utf-8"))


def make_claude(projects, slug: str, sid: str, cwd: str, text: str = "写个文件") -> None:
    folder = projects / slug
    folder.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "type": "user",
            "timestamp": "2026-10-20T10:00:00.000Z",
            "cwd": cwd,
            "sessionId": sid,
            "message": {"role": "user", "content": text},
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-20T10:00:05.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "想一下"},
                    {"type": "text", "text": "好了。"},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-10-20T10:00:06.000Z",
            "isSidechain": True,
            "message": {"role": "user", "content": "子代理的悄悄话"},
        },
    ]
    (folder / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )


def make_codex(sessions_root, sid: str, cwd: str, text: str = "写个文件") -> None:
    folder = sessions_root / "2026" / "10" / "20"
    folder.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2026-10-20T10:00:00.000Z",
            "type": "session_meta",
            "payload": {"session_id": sid, "cwd": cwd},
        },
        {
            "timestamp": "2026-10-20T10:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "系统指令"}],
            },
        },
        {
            "timestamp": "2026-10-20T10:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        {
            "timestamp": "2026-10-20T10:00:03.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "好了。"}],
            },
        },
    ]
    (folder / f"rollout-2026-10-20T10-00-00-{sid}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "sodus"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    dsh_root = tmp_path / "dshsessions"
    dsh_root.mkdir()
    monkeypatch.setattr(dsh, "ROOT", dsh_root)
    monkeypatch.setattr(config, "session_roots", lambda: [])

    claude_projects = home / "profiles" / "default" / "claude" / "relay" / "projects"
    codex_sessions = home / "profiles" / "default" / "codex" / "relay-openai" / "sessions"
    # only the fixture's stores: the real ones on this machine must not leak in
    monkeypatch.setattr(claude, "roots", lambda: [(claude_projects, "default", "relay")])
    monkeypatch.setattr(codex, "roots", lambda: [(codex_sessions, "default", "relay-openai")])

    make_claude(claude_projects, "D--work-demo", "aaaa1111-0000-7000-8000-000000000001",
                "D:\\work\\demo")
    make_codex(codex_sessions, "bbbb2222-0000-7000-8000-000000000002", "D:\\work\\demo")
    make_dsh(dsh_root, "--D-work-demo--", "session-cccc3333", "D:\\work\\demo")
    return type("World", (), {"home": home, "dsh": dsh_root, "claude": claude_projects})


# ---- read --------------------------------------------------------------


def test_every_vendor_shows_up_in_one_list(world):
    rows = sessions.list_all()
    assert {s.vendor for s in rows} == {"claude", "codex", "dsh"}
    assert len(rows) == 3


def test_a_transcript_is_normalised_across_vendors(world):
    for vendor in ("claude", "codex", "dsh"):
        found = sessions.list_all(vendor=vendor)
        assert found, vendor
        msgs = sessions.messages_of(found[0])
        assert [m["role"] for m in msgs] == [model.USER, model.ASSISTANT], vendor
        assert "好了" in msgs[-1]["text"]


def test_reasoning_is_opt_in(world):
    for vendor in ("claude", "dsh"):
        session = sessions.list_all(vendor=vendor)[0]
        with_reasoning = sessions.messages_of(session, include_reasoning=True)
        assert any(m["role"] == model.REASONING for m in with_reasoning), vendor


def test_system_noise_never_reaches_the_reader(world):
    for vendor in ("claude", "codex", "dsh"):
        session = sessions.list_all(vendor=vendor)[0]
        text = "\n".join(m["text"] for m in sessions.messages_of(session))
        assert "噪音" not in text and "子代理" not in text and "系统指令" not in text


def test_prefix_find_works_and_ambiguity_is_named(world):
    make_dsh(world.dsh, "--other--", "session-cccc4444", "D:\\work\\demo")
    assert sessions.find("cccc3333").vendor == "dsh"
    assert sessions.find("session-cccc3333").vendor == "dsh"
    assert sessions.find("dsh:session-cccc3333").vendor == "dsh"
    with pytest.raises(ValueError, match="ambiguous"):
        sessions.find("cccc")


def test_vendor_prefix_disambiguates(world):
    assert sessions.find("claude:aaaa1111").vendor == "claude"
    assert sessions.find("codex:bbbb2222").vendor == "codex"


def test_transcript_says_where_a_session_came_from(world):
    text = sessions.transcript("dsh:cccc3333")
    # DSH has no per-endpoint config home, so its origin is just the vendor
    assert "端点：dsh?" in text
    assert "dsh:session-cccc3333" in text


def test_brief_keeps_the_source_words_and_links(world):
    text = sessions.brief("dsh:cccc3333", "把这件事接着做")
    assert "把这件事接着做" in text
    assert "来源会话：dsh:session-cccc3333" in text
    assert "https://example.com/x" in text
    assert "未改写" in text


# ---- run linkage -------------------------------------------------------


def test_a_session_from_a_run_says_so(world):
    task = db.create_task("t")
    run = db.create_run(task["id"], "dsh", "D:\\work\\demo")
    db.update_run(
        run["id"],
        session_ref_json=json.dumps(
            {"provider": "dsh", "session_id": "session-cccc3333", "cwd": "D:\\work\\demo"}
        ),
    )
    found = sessions.find("cccc3333")
    assert found.run_id == run["id"]
    assert found.source == "run"


def test_a_run_whose_session_file_is_gone_still_appears(world):
    task = db.create_task("t")
    run = db.create_run(task["id"], "claude", "D:\\gone")
    db.update_run(
        run["id"],
        session_ref_json=json.dumps({"provider": "claude", "session_id": "dead-9999"}),
    )
    rows = sessions.list_all()
    orphans = [s for s in rows if s.vendor == "claude" and s.id == "dead-9999"]
    assert orphans and orphans[0].run_id == run["id"]
    assert "已经不在了" in sessions.transcript("dead-9999")


def test_orphans_obey_the_vendor_filter(world):
    """`--vendor dsh` is a promise about every row, including reconstructed runs."""
    task = db.create_task("t")
    run = db.create_run(task["id"], "claude", "D:\\gone")
    db.update_run(
        run["id"],
        session_ref_json=json.dumps({"provider": "claude", "session_id": "dead-9999"}),
    )
    rows = sessions.list_all(vendor="dsh")
    assert rows and {s.vendor for s in rows} == {"dsh"}
    assert "dead-9999" not in {s.id for s in rows}


# ---- the read whitelist ------------------------------------------------


def test_session_roots_restrict_what_can_be_read(world, tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    make_dsh(world.dsh, "--other--", "session-dddd4444", str(tmp_path / "elsewhere"))
    make_dsh(world.dsh, "--in--", "session-eeee5555", str(allowed / "proj"))
    monkeypatch.setattr(config, "session_roots", lambda: [allowed.resolve()])
    ids = {s.id for s in sessions.list_all()}
    assert "session-eeee5555" in ids
    assert "session-dddd4444" not in ids


# ---- continue / branch -------------------------------------------------


def test_continue_plans_the_endpoint_that_wrote_the_session(world):
    # the fixture session has no run behind it, so it counts as the user's own:
    # branch by default, but the endpoint is still recovered from its path
    branched = sessions.plan("claude:aaaa1111")
    assert branched["mode"] == "fork"
    assert branched["provider"] == "relay"
    assert branched["profile"] == "default"
    assert branched["agent"] == "claude"

    inplace = sessions.plan("claude:aaaa1111", in_place=True)
    assert inplace["mode"] == "resume"
    assert inplace["provider"] == "relay"


def test_continue_refuses_a_session_outside_our_profiles(tmp_path, monkeypatch):
    home = tmp_path / "sodus"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "session_roots", lambda: [])
    user_projects = home / "user-projects"
    monkeypatch.setattr(claude, "roots", lambda: [(user_projects, "", "")])
    make_claude(user_projects, "x", "ffff6666-0000-7000-8000-000000000006", "D:\\mine")
    with pytest.raises(ValueError, match="不是 Sodus 建的"):
        sessions.plan("claude:ffff6666")


def test_your_own_session_is_branched_by_default(world):
    """Resuming it would append a turn to a conversation you are still having."""
    plan = sessions.plan("dsh:cccc3333")
    assert plan["mode"] == "seed"
    assert "--in-place" in plan["note"]


def test_your_own_session_can_still_be_continued_explicitly(world):
    plan = sessions.plan("dsh:cccc3333", in_place=True)
    assert plan["mode"] == "resume"
    assert plan["session_id"] == "session-cccc3333"


def test_a_run_owned_session_is_continued_in_place(world):
    task = db.create_task("t")
    run = db.create_run(task["id"], "dsh", "D:\\work\\demo")
    db.update_run(
        run["id"],
        session_ref_json=json.dumps({"provider": "dsh", "session_id": "session-cccc3333"}),
    )
    plan = sessions.plan("dsh:cccc3333")
    assert plan["mode"] == "resume"
    assert plan["session"].run_id == run["id"]


def test_handing_a_conversation_to_another_vendor_is_a_seed(world):
    """No CLI can resume another's session: the id is resolved out of its own store."""
    plan = sessions.plan("claude:aaaa1111", fork=True, into="dsh")
    assert plan["mode"] == "seed"
    assert plan["agent"] == "dsh"
    assert plan["session_id"] == ""
    assert plan["cross"] is True
    assert "不是接续" in plan["note"]


def test_a_seed_needs_no_endpoint_for_the_source(world, monkeypatch, tmp_path):
    """A claude session outside our profiles can still be read and handed over."""
    from sodus.sessions import claude

    home = tmp_path / "other"
    monkeypatch.setattr(claude, "roots", lambda: [(home, "", "")])
    make_claude(home, "x", "ffff7777-0000-7000-8000-000000000007", "D:\\mine")
    plan = sessions.plan("claude:ffff7777", fork=True, into="dsh")
    assert plan["mode"] == "seed"
    with pytest.raises(ValueError, match="不是 Sodus 建的"):
        sessions.plan("claude:ffff7777")


def test_into_the_same_vendor_is_just_a_fork(world):
    plan = sessions.plan("claude:aaaa1111", fork=True, into="claude")
    assert plan["mode"] == "fork"
    assert "cross" not in plan


def test_a_dsh_branch_is_seeded_not_forked(world):
    plan = sessions.plan("dsh:cccc3333", fork=True)
    assert plan["mode"] == "seed"
    assert plan["session_id"] == ""
    assert "brief 播种" in plan["note"]


# ---- argv --------------------------------------------------------------


def _provider() -> providers.Provider:
    return providers.Provider(name="relay", protocol=providers.ANTHROPIC, model="m")


def test_claude_continue_and_branch_argv():
    resume = agents.argv(agents.SPECS["claude"], _provider(), session_id="abc")
    assert resume[-2:] == ["--resume", "abc"]
    fork = agents.argv(agents.SPECS["claude"], _provider(), session_id="abc", session_mode="fork")
    assert fork[-1] == "--fork-session"
    assert fork[-3:-1] == ["--resume", "abc"]


def test_codex_continue_and_branch_use_a_subcommand():
    openai = providers.Provider(name="relay-openai", protocol=providers.OPENAI, model="m")
    resume = agents.argv(agents.SPECS["codex"], openai, session_id="abc")
    at = resume.index("exec")
    assert resume[at : at + 3] == ["exec", "resume", "abc"]
    assert "--json" in resume
    fork = agents.argv(agents.SPECS["codex"], openai, session_id="abc", session_mode="fork")
    at = fork.index("exec")
    assert fork[at : at + 3] == ["exec", "fork", "abc"]


def test_dsh_continue_keeps_the_stdin_marker_last():
    deepseek = providers.Provider(name="deepseek", protocol=providers.DEEPSEEK)
    argv = agents.argv(agents.SPECS["dsh"], deepseek, session_id="session-x")
    assert argv[-1] == "-"
    assert argv[-3:-1] == ["--session-id", "session-x"]


def test_dsh_refuses_a_native_fork():
    deepseek = providers.Provider(name="deepseek", protocol=providers.DEEPSEEK)
    with pytest.raises(ValueError, match="no native fork"):
        agents.argv(agents.SPECS["dsh"], deepseek, session_id="s", session_mode="fork")
