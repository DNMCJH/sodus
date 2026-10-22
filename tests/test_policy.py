"""The policy layer: rules first, Jev as a reviewer, no invented evidence."""

from __future__ import annotations

import json

import pytest

from nodus import config, db, decision, policy
from nodus.cli import build_parser, main


# ---- fixtures ----------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A state directory per test, so record() and decisions.jsonl stay local."""
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-policy-test")
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture()
def rules_only(monkeypatch):
    """No Jev key: the caller must say `rules-only`, not fake a model pass."""
    monkeypatch.setattr(decision, "available", lambda: (False, "TYPESAFE_API_KEY not set"))


@pytest.fixture()
def reports(tmp_path, monkeypatch):
    """Do not let a future bench report on this machine change a unit test."""
    path = tmp_path / "reports"
    path.mkdir()
    monkeypatch.setattr(policy, "REPORTS", path)
    return path


def assess_rules(text: str, rules_only) -> dict:
    return policy.assess(text)


# ---- difficulty --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("给 README 改个错别字", 1, 1),
        ("把 foo 重命名为 bar", 1, 2),
        ("把 README 里的安装步骤更新一下，补上 Windows 的说明", 2, 2),
        ("修一下 login.py 里 token 过期没刷新的 bug", 2, 3),
        ("把 nodus/db.py 里的连接管理重构成上下文管理器，保证测试全绿", 4, 4),
        ("设计一个跨模块的缓存层，处理并发和失效", 4, 5),
        ("部署到服务器并重启 daemon，确认 4199 端口能访问", 2, 3),
        ("分析最近 7 天的 run 成本，找出异常并给出结论", 3, 3),
        ("把这份 3000 字的访谈整理成结构化笔记", 2, 3),
        ("从 docs/vision.md 里找出四层模型的原文", 1, 2),
        ("检查所有接口的鉴权，找出越权风险", 4, 5),
        ("查一下 db.py 里的连接函数在哪", 1, 2),
        ("把整个仓库的 pytest 全部跑一遍，把失败列出来", 3, 3),
    ],
)
def test_difficulty_bands(text, low, high, rules_only):
    result = assess_rules(text, rules_only)
    assert low <= result["difficulty"] <= high, (text, result)


def test_a_typo_and_a_refactor_are_four_steps_apart(rules_only):
    typo = assess_rules("给 README 改个错别字", rules_only)
    refactor = assess_rules(
        "把 nodus/db.py 里的连接管理重构成上下文管理器，保证测试全绿", rules_only
    )
    assert typo["difficulty"] == 1
    assert refactor["difficulty"] == 4


# ---- kind --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("给 README 改个错别字", "writing"),
        ("把 README 里的安装步骤更新一下", "writing"),
        ("把 foo 重命名为 bar", "code"),
        ("把 nodus/db.py 里的连接管理重构成上下文管理器，保证测试全绿", "code"),
        ("修一下 login.py 里 token 过期没刷新的 bug", "code"),
        ("把整个仓库的 pytest 全部跑一遍，把失败列出来", "code"),
        ("分析最近 7 天的 run 成本，找出异常并给出结论", "analysis"),
        ("检查所有接口的鉴权，找出越权风险", "analysis"),
        ("解释一下这个报错是怎么来的", "analysis"),
        ("从 docs/vision.md 里找出四层模型的原文", "retrieval"),
        ("查一下 db.py 里的连接函数在哪", "retrieval"),
        ("部署到服务器并重启 daemon，确认 4199 端口能访问", "ops"),
    ],
)
def test_kind_table(text, kind, rules_only):
    assert assess_rules(text, rules_only)["kind"] == kind


def test_vision_is_recognised_as_a_need(rules_only):
    result = assess_rules("看这张界面截图，找出登录按钮错在哪", rules_only)
    assert "vision" in result["needs"]


# ---- Jev: review, never authority --------------------------------------


def test_without_jev_the_basis_is_rules_only(rules_only):
    result = assess_rules("把 nodus/db.py 里的连接管理重构成上下文管理器", rules_only)
    assert result["basis"] == "rules-only"
    assert result["jev"]["ok"] is False
    assert "TYPESAFE_API_KEY" in result["jev"]["unavailable"]


def test_jev_reviews_the_rules_and_every_call_is_logged(home, monkeypatch):
    monkeypatch.setattr(decision, "available", lambda: (True, "ok"))
    monkeypatch.setattr(
        decision,
        "decide",
        lambda state, questions: {
            "ok": True,
            "model": "jev-test",
            "latency_ms": 7,
            "answers": {
                "difficulty": {"score": 0.0, "confidence": 1.0},
                "kind": {"choice": "writing", "confidence": 1.0},
                "needs_tool_use": {"noul": 0.9},
                "needs_long_context": {"noul": 0.1},
                "needs_reasoning": {"noul": 0.1},
                "needs_vision": {"noul": 0.0},
            },
        },
    )

    result = policy.assess("给 README 改个错别字")

    assert result["basis"] == "rules+jev"
    assert result["difficulty"] == 1
    assert result["kind"] == "writing"
    assert result["needs"] == ["tool_use"]
    assert result["jev"]["model"] == "jev-test"
    assert result["jev"]["agreement"]["difficulty"] is True
    assert result["jev"]["agreement"]["kind"] is True
    assert result["confidence"] > result["rules"]["confidence"]

    lines = (home / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["kind"] == "policy.assess"


def test_jev_can_raise_difficulty_but_only_by_one_step(home, monkeypatch):
    monkeypatch.setattr(decision, "available", lambda: (True, "ok"))
    monkeypatch.setattr(
        decision,
        "decide",
        lambda state, questions: {
            "ok": True,
            "model": "jev-test",
            "latency_ms": 5,
            "answers": {
                "difficulty": {"score": 4.0, "confidence": 0.95},
                "kind": {"choice": "code", "confidence": 0.95},
            },
        },
    )
    result = policy.assess("把 foo 重命名为 bar")
    assert result["rules"]["difficulty"] == 1
    assert result["difficulty"] == 2


def test_a_failed_jev_call_falls_back_to_rules(home, monkeypatch):
    monkeypatch.setattr(decision, "available", lambda: (True, "ok"))
    monkeypatch.setattr(
        decision,
        "decide",
        lambda state, questions: {"ok": False, "unavailable": "network: timed out"},
    )
    result = policy.assess("把 nodus/db.py 里的连接管理重构成上下文管理器")
    assert result["basis"] == "rules-only"
    assert result["jev"]["available"] is True
    assert result["jev"]["ok"] is False
    assert "timed out" in result["jev"]["unavailable"]


def test_an_empty_task_is_rejected(rules_only):
    with pytest.raises(ValueError, match="empty"):
        policy.assess("   ")


# ---- recommendation and evidence --------------------------------------


def test_every_recommendation_names_a_real_route(home, rules_only, reports):
    texts = [
        "给 README 改个错别字",
        "把 nodus/db.py 里的连接管理重构成上下文管理器，保证测试全绿",
        "部署到服务器并重启 daemon",
        "看这张界面截图，找出登录按钮错在哪",
        "分析最近 7 天的 run 成本",
    ]
    for text in texts:
        rec = policy.recommend(policy.assess(text))
        assert rec["route"] in policy.ROUTE_NAMES, (text, rec)
        assert rec["alternatives"], text
        for alt in rec["alternatives"]:
            assert alt["route"] in policy.ROUTE_NAMES
            assert alt["route"] != rec["route"]
            assert alt["why"]
            assert alt["evidence"]["status"] in ("measured", "unknown", "unsupported")


def test_routes_without_measurements_carry_unknown(home, rules_only, reports):
    for route in ("claude", "minimax", "codex", "dsh"):
        assert policy.evidence_for(route)["status"] == "unknown"

    rec = policy.recommend(
        policy.assess("把 nodus/db.py 里的连接管理重构成上下文管理器，保证测试全绿")
    )
    assert rec["route"] == "claude"
    assert rec["evidence"]["status"] == "unknown"
    assert "claude" in rec["unmeasured"]
    assert "没有实测" in rec["rationale"]
    for alt in rec["alternatives"]:
        if alt["evidence"]["status"] == "unknown":
            assert alt["route"] in rec["unmeasured"]


def test_the_measured_ab_is_quoted_not_invented(home, rules_only, reports):
    glm = policy.evidence_for("opencode-glm")
    assert glm["status"] == "measured"
    assert glm["stats"]["mean_s"] == 12.4
    assert glm["stats"]["speedup_vs_gemini_pct"] == 17
    assert glm["stats"]["key_points"] == "10/12"

    gemini = policy.evidence_for("opencode-gemini")
    assert gemini["status"] == "measured"
    assert gemini["stats"]["mean_s"] == 15.0
    assert "not repo-scale" in glm["scope"]


def test_a_bench_result_becomes_measured_evidence(home, reports):
    (reports / "bench-claude.json").write_text(
        json.dumps(
            {
                "route": "claude",
                "totals": {
                    "attempts": 6,
                    "solved": 5,
                    "mean_s": 20.0,
                    "cost_cny": 0.12,
                },
            }
        ),
        encoding="utf-8",
    )
    evidence = policy.evidence_for("claude")
    assert evidence["status"] == "measured"
    assert evidence["stats"]["solved"] == 5
    assert evidence["stats"]["solve_rate"] == pytest.approx(0.833, abs=0.001)


def test_the_same_task_family_gets_a_measured_alternative(home, rules_only, reports):
    rec = policy.recommend(
        policy.assess("把 nodus/db.py 里的连接管理重构成上下文管理器，保证测试全绿")
    )
    measured = [alt for alt in rec["alternatives"] if alt["evidence"]["status"] == "measured"]
    assert measured, rec
    assert any(alt["route"] in ("opencode-glm", "opencode-gemini") for alt in measured)


def test_trivial_and_hard_tasks_do_not_get_the_same_route(home, rules_only, reports):
    typo = policy.recommend(policy.assess("给 README 改个错别字"))
    hard = policy.recommend(
        policy.assess("把 nodus/db.py 里的连接管理重构成上下文管理器，保证测试全绿")
    )
    assert typo["route"] == "dsh"
    assert hard["route"] == "claude"
    assert typo["route"] != hard["route"]


def test_vision_goes_to_the_route_the_ledger_assigned_and_stays_unknown(home, rules_only, reports):
    rec = policy.recommend(policy.assess("看这张界面截图，找出登录按钮错在哪"))
    assert rec["route"] == "opencode-gemini"
    assert rec["evidence"]["status"] == "unknown"
    assert "视觉" in rec["evidence"]["note"] or "多模态" in rec["evidence"]["note"]


# ---- record: why did this go there? -----------------------------------


def test_record_writes_an_event_a_note_and_a_ledger_line(home, rules_only, reports):
    task = db.create_task(goal="refactor the connection manager", acceptance="pytest -q")
    run = db.create_run(task["id"], "claude", str(home), route="claude")
    assessment = policy.assess("把 nodus/db.py 里的连接管理重构成上下文管理器，保证测试全绿")
    recommendation = policy.recommend(assessment)

    written = policy.record(run["id"], assessment, recommendation)

    assert written["ok"] is True
    assert written["event"] == "policy.decision"
    events = [e for e in db.events_after(run["id"]) if e["type"] == "policy.decision"]
    assert len(events) == 1
    assert events[0]["payload"]["assess"]["difficulty"] == assessment["difficulty"]
    assert events[0]["payload"]["recommend"]["route"] == recommendation["route"]

    note = home / "runs" / run["id"] / "policy.json"
    saved = json.loads(note.read_text(encoding="utf-8"))
    assert saved["assess"]["kind"] == assessment["kind"]
    assert saved["recommend"]["route"] == recommendation["route"]

    ledger = [
        json.loads(line)
        for line in (home / "policy.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert ledger[-1]["run_id"] == run["id"]
    assert ledger[-1]["task_id"] == task["id"]


def test_record_without_a_run_still_keeps_the_ledger(home, rules_only, reports):
    assessment = policy.assess("给 README 改个错别字")
    recommendation = policy.recommend(assessment)
    written = policy.record("", assessment, recommendation)
    assert written["ok"] is True
    assert written["event"] == ""
    assert (home / "policy.jsonl").is_file()


def test_record_says_who_chose_and_what_actually_ran(home, rules_only, reports):
    task = db.create_task(goal="fix the typo in README")
    run = db.create_run(task["id"], "codex", str(home), route="codex")
    assessment = policy.assess("给 README 改个错别字")
    recommendation = policy.recommend(assessment)

    policy.record(
        run["id"],
        assessment,
        recommendation,
        chosen_by="operator",
        chosen_route="codex",
        requested_route="codex",
    )

    event = next(e for e in db.events_after(run["id"]) if e["type"] == "policy.decision")
    assert event["payload"]["chosen_by"] == "operator"
    assert event["payload"]["chosen_route"] == "codex"
    assert event["payload"]["requested_route"] == "codex"


# ---- local run history: success, speed and real cost -------------------


def _finish(
    task_id: str,
    route: str,
    status: str,
    *,
    seconds: float = 4.0,
    cost: float | None = None,
    currency: str = "CNY",
    verify: str | None = None,
    routed: bool = True,
):
    """Create a finished run row, how the dispatcher leaves one behind."""
    run = db.create_run(
        task_id,
        route,
        str(config.HOME),
        route=route if routed else "",
    )
    fields = {"status": status, "ended_at": run["started_at"] + seconds}
    if cost is not None:
        fields["usage_json"] = json.dumps(
            {"cost": cost, "currency": currency, "cost_basis": "metered"}
        )
    db.update_run(run["id"], **fields)
    if verify:
        db.append_event(
            run["id"], db.next_seq(run["id"]), f"verify.{verify}", {"phase": verify}
        )
    return run


def test_local_history_counts_success_cost_and_speed(home, rules_only, reports):
    task = db.create_task(goal="fix the login bug")
    _finish(task["id"], "codex", "completed", cost=0.01, verify="green")
    _finish(task["id"], "codex", "completed", cost=0.01, verify="green")
    # Exit 0 with nothing verified is not a success; this project's whole point.
    _finish(task["id"], "codex", "completed", cost=0.01)
    _finish(task["id"], "codex", "failed", cost=0.02, verify="red")
    _finish(task["id"], "codex", "failed", cost=0.002, currency="USD")

    evidence = policy.evidence_for("codex")

    assert evidence["status"] == "measured"
    stats = evidence["stats"]
    assert stats["runs"] == 5
    assert stats["completed"] == 3
    assert stats["verified_green"] == 2
    assert stats["success_rate"] == 0.4
    assert stats["mean_s"] == 4.0
    assert stats["cost"]["CNY"]["runs"] == 4
    assert stats["cost"]["CNY"]["mean"] == pytest.approx(0.0125)
    assert stats["cost"]["USD"]["runs"] == 1
    assert stats["cost"]["USD"]["mean"] == pytest.approx(0.002)
    assert "不换算汇率" in evidence["note"]
    assert evidence["history"]["samples"] == 5


def test_two_runs_are_not_enough_to_conclude(home, rules_only, reports):
    task = db.create_task(goal="write the release note")
    _finish(task["id"], "dsh", "completed", verify="green")
    _finish(task["id"], "dsh", "failed")

    evidence = policy.evidence_for("dsh")

    assert evidence["status"] == "insufficient"
    assert evidence["history"]["status"] == "insufficient"
    assert evidence["history"]["samples"] == 2
    assert evidence["history"]["stats"]["success_rate"] == 0.5
    assert evidence["stats"] == {}


def test_no_history_is_unknown_not_insufficient(home, rules_only, reports):
    assert policy.evidence_for("dsh")["history"]["status"] == "none"
    assert policy.evidence_for("dsh")["status"] == "unknown"


def test_history_window_keeps_only_the_most_recent_runs(home, rules_only, reports):
    task = db.create_task(goal="fix the login bug")
    for _ in range(3):
        _finish(task["id"], "codex", "completed", verify="green")

    history = policy._run_history("codex", limit=2)

    assert history["samples"] == 2
    assert history["window"] == 2
    assert history["stats"]["runs"] == 2


def test_runs_from_before_the_route_column_still_count(home, rules_only, reports):
    task = db.create_task(goal="fix the login bug")
    _finish(task["id"], "codex", "completed", verify="green", routed=False)
    _finish(task["id"], "codex", "completed", verify="green")
    _finish(task["id"], "codex", "failed")

    assert policy._run_history("codex")["samples"] == 3


def test_operator_route_assessment_does_not_spend_a_jev_call(home, monkeypatch):
    monkeypatch.setattr(decision, "available", lambda: (True, "ok"))

    def explode(state, questions):
        raise AssertionError("assess(review=False) must not call Jev")

    monkeypatch.setattr(decision, "decide", explode)

    result = policy.assess("给 README 改个错别字", review=False)

    assert result["basis"] == "rules-only"
    assert result["jev"]["ok"] is False
    assert "operator" in result["jev"]["unavailable"]
    assert not (home / "decisions.jsonl").exists()


# ---- CLI ---------------------------------------------------------------


def test_route_for_is_registered_and_does_not_need_a_daemon():
    import argparse

    parser = build_parser()
    sub = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    command = sub.choices["route-for"]
    assert command.get_default("func") is not None
    assert command.get_default("needs_daemon") is False


def test_route_for_json_is_machine_readable(capsys, home, rules_only, reports):
    assert main(["route-for", "给 README 改个错别字", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["assess"]["basis"] == "rules-only"
    assert payload["assess"]["difficulty"] == 1
    assert payload["recommend"]["route"] == "dsh"


def test_route_for_apply_prints_the_next_command_not_a_dispatch(capsys, home, rules_only, reports):
    assert main(["route-for", "给 README 改个错别字", "--json", "--apply"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"]["dispatched"] is False
    assert payload["apply"]["route"] == "dsh"
    assert "--route dsh" in payload["apply"]["command"]


def test_history_separates_finished_from_verified(monkeypatch, tmp_path):
    """A repo with no test command must not make every route look useless.

    `success_rate` is green/samples, which is 0 for any project that never has
    a test command. The routing decision needs two numbers instead: how often
    the run finishes, and -- among runs that were actually checked -- how often
    it goes green.
    """
    from nodus import db, policy

    ids = []
    task = db.create_task("history-shape")
    # A route name nobody else uses, so the assertion is about these three runs
    # and not about whatever history the real workspace already has.
    route = "pytest-history-shape"
    import time as _time

    for index, status in enumerate(("completed", "completed", "failed")):
        run = db.create_run(task_id=task["id"], agent="claude", cwd=".", route=route)
        started = _time.time() - 1000
        # durations 10s / 20s / 30s -> median 20s, mean 20s (no outliers here)
        db.update_run(
            run["id"],
            status=status,
            started_at=started,
            ended_at=started + 10 * (index + 1),
        )
        ids.append(run["id"])
    try:
        history = policy._run_history(route)
        stats = history["stats"]
        assert stats["finished_rate"] == round(2 / 3, 3)
        # none of these runs was verified, so the verified rate is unknown
        assert stats["verified_samples"] == 0
        assert stats["verified_ok_rate"] is None
        assert stats["median_s"] is not None
    finally:
        conn = db.connect()
        for rid in ids:
            conn.execute("DELETE FROM run WHERE id=?", (rid,))
        conn.execute("DELETE FROM task WHERE id=?", (task["id"],))
        conn.commit()
        conn.close()


# ---- scoring: does the price actually change the route? ----------------
#
# The scorer is a pure function over per-route facts, so these tests feed it
# synthetic measurements instead of depending on whatever bench happens to be
# in `reports/`.


def _synth(
    route: str,
    *,
    solved: int = 3,
    attempts: int = 3,
    seconds: float | None = None,
    cost: float | None = None,
    tested: int = 4,
    measured_by: str = "bench",
    status: str = "measured",
    tier: int | None = 2,
) -> dict:
    """One route's scoring facts, shaped like `policy._measure` returns them."""
    return {
        "route": route,
        "status": status,
        "measured_by": measured_by if attempts else "",
        "solve_rate": (solved / attempts) if attempts else None,
        "solved": solved if attempts else None,
        "attempts": attempts or None,
        "seconds": seconds,
        "cost_cny": cost,
        "cost_known": bool(cost is not None and cost > 0),
        "tested_difficulty": tested if measured_by == "bench" else None,
        "declared_cost_tier": tier,
        "note": "",
    }


def test_scoring_takes_the_cheap_route_when_it_solves_the_same():
    measures = {
        "claude": _synth("claude", seconds=66.0, cost=0.30, tested=4),
        "minimax": _synth("minimax", seconds=30.0, cost=0.03, tested=4),
    }
    route, _, scoring = policy._choose(2, "code", measures)

    rows = {row["route"]: row for row in scoring["candidates"]}
    assert route == "minimax"
    assert rows["claude"]["cost_penalty"] > rows["minimax"]["cost_penalty"]
    assert rows["minimax"]["score"] > rows["claude"]["score"]
    assert any(rule["rule"] == "low-difficulty-cheap" for rule in scoring["guardrails"])


def test_scoring_pays_for_the_stronger_route_on_a_hard_task():
    measures = {
        "claude": _synth("claude", solved=3, seconds=66.0, cost=0.30, tested=4, tier=3),
        "minimax": _synth("minimax", solved=1, seconds=30.0, cost=0.03, tested=4),
    }
    route, _, scoring = policy._choose(4, "code", measures)

    rows = {row["route"]: row for row in scoring["candidates"]}
    assert route == "claude"
    assert rows["claude"]["measured_solve_bonus"] > rows["minimax"]["measured_solve_bonus"]
    assert any(rule["rule"] == "high-difficulty-proven" for rule in scoring["guardrails"])


def test_a_single_bad_run_does_not_eliminate_a_route(home, rules_only, reports):
    task = db.create_task(goal="one bad run")
    _finish(task["id"], "codex", "failed", verify="red")

    evidence = policy.evidence_for("codex")
    measure = policy._measure("codex", evidence)

    assert evidence["status"] == "insufficient"
    assert measure["solve_rate"] is None
    rows, _ = policy._score_candidates(
        3,
        "code",
        {"codex": measure, "minimax": _synth("minimax", seconds=30.0, cost=0.03)},
    )
    codex = next(row for row in rows if row["route"] == "codex")
    assert codex["excluded"] == ""
    assert codex["solve_rate"] is None


def test_unsupported_routes_are_excluded_from_scoring(home, rules_only, reports):
    evidence = policy.evidence_for("claude", need="vision")
    assert evidence["status"] == "unsupported"

    measures = {
        "claude": policy._measure("claude", evidence),
        "opencode-gemini": _synth(
            "opencode-gemini", attempts=0, seconds=15.0, measured_by="ab", status="unknown"
        ),
    }
    route, _, scoring = policy._choose(2, "writing", measures)
    rows = {row["route"]: row for row in scoring["candidates"]}

    assert rows["claude"]["excluded"]
    assert route == "opencode-gemini"


def test_a_missing_price_is_unknown_not_free():
    measures = {
        "dsh": _synth("dsh", seconds=40.0, cost=None, tier=1),
        "claude": _synth("claude", seconds=66.0, cost=0.30, tier=3),
        "opencode-glm": _synth("opencode-glm", seconds=50.0, cost=0.02, tier=None),
    }
    rows, _ = policy._score_candidates(1, "writing", measures)
    by_route = {row["route"]: row for row in rows}

    dsh = by_route["dsh"]
    # Two known penalties: claude ~0.457, glm 0. The median is ~0.228, not 0.
    assert dsh["cost_known"] is False
    assert dsh["cost_penalty"] > 0
    assert dsh["cost_basis"].startswith("unknown")
    assert dsh["cost_penalty"] == pytest.approx(0.228, abs=0.01)


def test_bench_outranks_live_history_for_comparison(home, rules_only, reports):
    """A controlled 3-task bench beats this repo's self-selected run history."""
    task = db.create_task(goal="same task on every route")
    for _ in range(3):
        _finish(task["id"], "claude", "completed", cost=9.0, verify="green")
    (reports / "bench-claude.json").write_text(
        json.dumps(
            {
                "route": "claude",
                "totals": {
                    "attempts": 3,
                    "solved": 2,
                    "mean_s": 10.0,
                    "cost_cny": 0.3,
                },
            }
        ),
        encoding="utf-8",
    )

    evidence = policy.evidence_for("claude")
    measure = policy._measure("claude", evidence)

    assert evidence["measured_by"] == "bench"
    assert evidence["stats"]["solve_rate"] == pytest.approx(0.667, abs=0.001)
    assert evidence["history"]["samples"] == 3
    assert measure["cost_cny"] == pytest.approx(0.1)  # 0.3 / 3, not the 9.0 run cost
    assert measure["solve_rate"] == pytest.approx(0.667, abs=0.001)


def test_bench_with_partial_pricing_keeps_cost_unknown(home, rules_only, reports):
    """deepseek has no price table: two of three runs record cost 0.0."""
    (reports / "bench-dsh.json").write_text(
        json.dumps(
            {
                "route": "dsh",
                "totals": {
                    "attempts": 3,
                    "solved": 3,
                    "mean_s": 40.7,
                    "cost_cny": 0.1,
                },
                "attempts": [
                    {"title": "修复 ranges 的差一错误", "cost": 0.0, "duration_s": 30.5},
                    {"title": "把 slug 逻辑抽成函数", "cost": 0.0, "duration_s": 24.8},
                    {"title": "写一个单位换算 CLI", "cost": 0.1, "duration_s": 66.9},
                ],
            }
        ),
        encoding="utf-8",
    )

    evidence = policy.evidence_for("dsh")
    measure = policy._measure("dsh", evidence)

    assert evidence["stats"]["cost_complete"] is False
    assert evidence["stats"]["priced_attempts"] == 1
    assert measure["cost_cny"] is None
    assert measure["cost_known"] is False
    assert "价格数据不完整" in evidence["note"]


def test_a_tie_goes_to_the_route_with_measurements(home, rules_only, reports):
    """Same score, one route measured and one not: measured wins the tie."""
    measures = {
        "claude": _synth(
            "claude", attempts=0, seconds=None, cost=None, measured_by="", status="unknown"
        ),
        "codex": _synth("codex", solved=0, seconds=None, cost=None, tested=3, status="measured"),
    }
    route, _, scoring = policy._choose(3, "code", measures)
    assert route == "codex"
    assert scoring["candidates"][0]["measured_by"] == "bench"


def test_every_score_is_the_sum_of_its_parts():
    measures = {
        "claude": _synth("claude", solved=2, seconds=66.0, cost=0.30, tested=4, tier=3),
        "codex": _synth("codex", seconds=62.5, cost=0.036),
        "opencode-glm": _synth("opencode-glm", seconds=52.5, cost=0.022, tier=None),
    }
    rows, _ = policy._score_candidates(3, "code", measures)
    for row in rows:
        assert row["score"] == pytest.approx(
            row["capability_fit"]
            + row["measured_solve_bonus"]
            - row["cost_penalty"]
            - row["time_penalty"],
            abs=0.001,
        )


def test_the_decision_event_carries_every_candidate_breakdown(home, rules_only, reports):
    task = db.create_task(goal="fix the login bug")
    run = db.create_run(task["id"], "codex", str(home), route="codex")
    assessment = policy.assess("修一下 login.py 里 token 过期没刷新的 bug")
    recommendation = policy.recommend(assessment)

    policy.record(run["id"], assessment, recommendation)

    payload = next(
        event["payload"]
        for event in db.events_after(run["id"])
        if event["type"] == "policy.decision"
    )
    scoring = payload["scoring"]
    candidates = {row["route"]: row for row in scoring["candidates"]}

    assert scoring["winner"] == recommendation["route"]
    assert payload["recommend"]["scoring"]["winner"] == recommendation["route"]
    assert set(candidates) == set(policy.ROUTE_NAMES)
    for row in candidates.values():
        for key in (
            "score",
            "capability_fit",
            "measured_solve_bonus",
            "cost_penalty",
            "time_penalty",
            "cost_basis",
            "time_basis",
            "excluded",
        ):
            assert key in row, (row["route"], key)


def test_route_for_text_shows_the_scoring_line(capsys, home, rules_only, reports):
    (reports / "bench-dsh.json").write_text(
        json.dumps(
            {
                "route": "dsh",
                "totals": {"attempts": 3, "solved": 3, "mean_s": 40.0, "cost_cny": 0.3},
            }
        ),
        encoding="utf-8",
    )
    assert main(["route-for", "给 README 改个错别字"]) == 0
    out = capsys.readouterr().out

    scoring_line = next(
        line for line in out.splitlines() if line.strip().startswith("打分")
    )
    assert "dsh" in scoring_line
    assert "minimax" in scoring_line
    assert "护栏" in out
