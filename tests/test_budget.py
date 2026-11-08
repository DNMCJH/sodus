"""The daily ceiling refuses new runs and never kills a running one."""

from __future__ import annotations

import pytest

import json

from nodus import budget, config, db


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate the database: budget tests must not read today's real spend.

    Without this the test compared its own ¥1.5 against whatever the machine
    had really spent today, so it went red whenever the workspace was busy
    enough -- a test that fails because of the user's afternoon.
    """
    from nodus import config

    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-budget-test")
    monkeypatch.delenv("NODUS_DAILY_BUDGET", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path


def _run(cost: float, currency: str = "CNY") -> str:
    created = db.create_run(task_id="budget-test", agent="claude", cwd=".")
    db.update_run(
        created["id"],
        usage_json=json.dumps({"cost": cost, "currency": currency}),
    )
    return created["id"]


def _clean(ids: list[str]) -> None:
    conn = db.connect()
    for rid in ids:
        conn.execute("DELETE FROM run WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def test_no_limit_means_no_refusal(monkeypatch):
    monkeypatch.setattr(budget, "limit", lambda: 0.0)
    info = budget.check()
    assert info["over"] is False and info["remaining"] is None


def test_limit_reads_the_environment_first(monkeypatch):
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "12.5")
    assert budget.limit() == 12.5
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "not-a-number")
    assert budget.limit() == 0.0


def test_spend_today_counts_only_cny(monkeypatch, home):
    monkeypatch.setattr(budget, "limit", lambda: 100.0)
    ids = [_run(1.5), _run(2.0, "USD")]
    try:
        # The USD run is excluded rather than converted at an invented rate.
        assert budget.spent() >= 1.5
        assert budget.spent() < 3.5
    finally:
        _clean(ids)


def test_over_the_limit_is_reported_with_numbers(monkeypatch):
    monkeypatch.setattr(budget, "limit", lambda: 1.0)
    monkeypatch.setattr(budget, "spent", lambda now=None: 2.5)
    info = budget.check()
    assert info["over"] is True and info["remaining"] == 0.0
    message = budget.refuse_message(info)
    assert "2.50" in message and "1.00" in message


def test_the_ceiling_refuses_new_runs(monkeypatch, home):
    monkeypatch.setattr(budget, "limit", lambda: 1.0)
    monkeypatch.setattr(budget, "spent", lambda now=None: 9.0)
    from nodus import runner

    try:
        runner.RUNNER.start(task_id="budget-refuse", route_name="claude", cwd=".")
    except RuntimeError as error:
        assert "daily ceiling" in str(error)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("a run was allowed past the daily ceiling")

def test_soft_limit_only_gates_expensive_routes(monkeypatch):
    monkeypatch.setattr(budget, "limit", lambda: 50.0)
    monkeypatch.setattr(budget, "soft_limit", lambda: 20.0)
    monkeypatch.setattr(budget, "expensive_routes", lambda: ("opus",))
    monkeypatch.setattr(budget, "spent", lambda now=None: 25.0)

    assert budget.check(route="sol")["gated"] is False
    assert budget.check(route="opus")["gated"] is True
    assert budget.check(route="opus", allow_expensive=True)["gated"] is False
    # The hard ceiling still wins over the opt-in.
    monkeypatch.setattr(budget, "limit", lambda: 20.0)
    assert budget.check(route="opus", allow_expensive=True)["over"] is True


def test_no_soft_limit_never_gates(monkeypatch):
    monkeypatch.setattr(budget, "limit", lambda: 50.0)
    monkeypatch.setattr(budget, "soft_limit", lambda: 0.0)
    monkeypatch.setattr(budget, "expensive_routes", lambda: ("opus",))
    monkeypatch.setattr(budget, "spent", lambda now=None: 49.0)
    assert budget.check(route="opus")["gated"] is False


def test_the_soft_limit_refuses_an_expensive_run(monkeypatch, home):
    monkeypatch.setattr(budget, "limit", lambda: 50.0)
    monkeypatch.setattr(budget, "soft_limit", lambda: 20.0)
    monkeypatch.setattr(budget, "expensive_routes", lambda: ("claude",))
    monkeypatch.setattr(budget, "spent", lambda now=None: 25.0)
    from nodus import runner

    try:
        runner.RUNNER.start(task_id="budget-soft", route_name="claude", cwd=".")
    except RuntimeError as error:
        assert "is expensive" in str(error)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("an expensive route ran past the soft limit")


def test_pinned_expensive_models_are_gated_too(monkeypatch):
    monkeypatch.setattr(budget, "limit", lambda: 50.0)
    monkeypatch.setattr(budget, "soft_limit", lambda: 20.0)
    monkeypatch.setattr(budget, "expensive_routes", lambda: ())
    monkeypatch.setattr(budget, "expensive_models", lambda: ("claude-opus",))
    monkeypatch.setattr(budget, "spent", lambda now=None: 25.0)

    # A cheap route with an expensive model pinned is still expensive.
    assert budget.check(route="sol", model="claude-opus-5-5")["gated"] is True
    assert budget.check(route="sol", model="gpt-6.1-sol")["gated"] is False
    assert (
        budget.check(route="sol", model="claude-opus-5-5", allow_expensive=True)["gated"]
        is False
    )



@pytest.mark.parametrize('model,route,family', [
    ('CLAUDE-4', '', 'claude'), ('opus-5', '', 'claude'), ('sonnet', '', 'claude'),
    ('haiku', '', 'claude'), ('gpt6', '', 'gpt'), ('vendor/gpt-6', '', 'gpt'),
    ('o1', '', 'gpt'), ('o3', '', 'gpt'), ('o4', '', 'gpt'), ('o5', '', 'gpt'),
    ('codex', '', 'gpt'), ('deepseek', 'opus', 'other'),
    ('', 'opus', 'claude'), ('', 'claude', 'claude'), ('', 'sonnet', 'claude'),
    ('', 'sol', 'gpt'), ('', 'codex', 'gpt'), ('', 'terra', 'gpt'), ('', 'dsh', 'other'),
])
def test_family_classification(model, route, family):
    assert budget.model_family(model, route) == family


def test_family_attempt_fallback():
    usage = json.dumps({'attempts': [{'model': 'gpt-6'}, {'model': 'claude'}]})
    assert budget.model_family('', 'opus', usage) == 'gpt'
    assert budget.model_family('haiku', 'sol', usage) == 'claude'
    assert budget.model_family('', 'sol', '{broken') == 'gpt'
    assert budget.model_family(agent='codex') == 'gpt'


def test_family_spend(home):
    rows = [
        ('haiku', 'sol', {'cost': 10, 'currency': 'CNY'}),
        ('', 'opus', {'cost': 7, 'currency': 'USD'}),
        ('', 'opus', {'cost': 2, 'attempts': [{'model': 'gpt-6'}]}),
        ('', 'dsh', {'cost': 3, 'currency': 'CNY'}),
        ('', 'opus', {}),
    ]
    for model, route, usage in rows:
        row = db.create_run('budget-test', 'claude', '.', route=route)
        db.update_run(row['id'], model=model, usage_json=json.dumps(usage))
    assert budget.spent_by_family() == {'claude': 10, 'gpt': 2, 'other': 3}
    assert sum(budget.spent_by_family().values()) == budget.spent()


@pytest.mark.parametrize('family,model,cap', [('claude', 'opus', 100), ('gpt', 'gpt-6', 80), ('other', 'deepseek', 80)])
def test_family_gate(home, monkeypatch, family, model, cap):
    monkeypatch.setattr(config, 'load_config', lambda: {'budget': {'soft_families': {'claude': 100, 'gpt': 80, 'other': 80}}})
    row = db.create_run('budget-test', 'codex', '.')
    db.update_run(row['id'], model=model, usage_json=json.dumps({'cost': cap - .01, 'currency': 'CNY'}))
    assert not budget.check(model=model)['family_gated']
    db.update_run(row['id'], usage_json=json.dumps({'cost': cap, 'currency': 'CNY'}))
    info = budget.check(model=model, agent='codex')
    assert info['family'] == family and info['family_limit'] == cap
    assert info['family_spent'] == cap and info['family_gated'] and info['gated']
    assert family in budget.blocked_message(info) and '--allow-expensive' in budget.blocked_message(info)
    assert not budget.check(model=model, allow_expensive=True)['gated']
    monkeypatch.setattr(config, 'load_config', lambda: {'budget': {'soft_families': {family: 0}}})
    assert not budget.check(model=model)['gated']
    monkeypatch.setattr(config, 'load_config', lambda: {})
    assert not budget.check(model=model)['gated']


def test_legacy_config(home, monkeypatch):
    monkeypatch.setattr(config, 'load_config', lambda: {'budget': {'daily_cny': 150, 'soft_cny': 50}})
    assert budget.limit() == 150 and budget.soft_limit() == 50
    assert budget.family_limits() == {'claude': 0, 'gpt': 0, 'other': 0}


def test_runner_checks_resolved_family_before_claim(home, monkeypatch):
    from nodus import runner, routes, profiles
    monkeypatch.setattr(config, 'load_config', lambda: {'budget': {'soft_families': {'gpt': 80}}})
    monkeypatch.setattr(budget, 'spent_by_family', lambda now=None: {'claude': 0, 'gpt': 80, 'other': 0})
    monkeypatch.setattr(profiles, 'ensure_ready', lambda: None)
    monkeypatch.setattr(profiles, 'read', lambda name: type('Profile', (), {'model': ''})())
    monkeypatch.setattr(runner.RUNNER, '_decide_route', lambda *a: {'route': 'custom'})
    monkeypatch.setattr(routes, 'resolve_for_run', lambda name: routes.Route(name='custom', agent='codex', providers=(), model='gpt-6'))
    task = db.create_task('family test', 'blocked')
    with pytest.raises(RuntimeError, match='gpt family'):
        runner.RUNNER.start(task['id'], route_name='auto', cwd=str(home))
    assert not db.runs_in_window(0)
