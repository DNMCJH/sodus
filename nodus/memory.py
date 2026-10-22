"""Facts that outlive a run, in three scopes.

`private` belongs to one agent and is invisible to every other agent — the
whole point of a secret is that the runner must not paste it into a brief
handed to somebody else. `project` belongs to the current workspace. `public`
is shared everywhere.

Matching is keyword `LIKE` plus a hit count, not embeddings: the corpus here is
a handful of confirmed facts per project, and a vector index would be a
dependency, a second store to keep in sync, and a failure mode with no upside
at this size. The ordering rule is the simple one — more query terms matched
beats fewer, then newer wins.
"""

from __future__ import annotations

import re
from typing import Any

from . import config, db

SCOPES = ("private", "project", "public")

#: Query terms are split on whitespace and punctuation, then CJK runs longer
#: than two characters also contribute their bigrams. Without the bigrams a
#: whole task sentence almost never matches a stored fact verbatim.
_SPLIT = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_MAX_TERMS = 32
#: A brief is a prompt, not a wiki: the memory block gets a hard character
#: budget so five long facts cannot double the size of every dispatch.
CONTEXT_BUDGET = 1200


def _tags(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = [str(value)]
    return ",".join(item.strip() for item in items if item and item.strip())


def _clip(text: str, limit: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _normalize_scope(scope: str | None, default: str = "project") -> str:
    value = (scope or default).strip().lower()
    if value not in SCOPES:
        raise ValueError(f"unknown memory scope: {scope} (use {', '.join(SCOPES)})")
    return value


def _visible(scope: str | None, agent: str) -> tuple[str, list[Any]]:
    """The SQL predicate that decides what this caller may see.

    Private rows never match an empty agent: `remember` refuses to create one
    without an owner, and `agent=?` with '' must stay a dead branch rather than
    a wildcard.
    """
    agent = (agent or "").strip()
    if scope is None:
        return (
            "(scope='public' OR (scope='project' AND workspace_id=?)"
            " OR (scope='private' AND agent=?))",
            [config.workspace_id(), agent],
        )
    chosen = _normalize_scope(scope)
    if chosen == "public":
        return "scope='public'", []
    if chosen == "project":
        return "scope='project' AND workspace_id=?", [config.workspace_id()]
    return "scope='private' AND agent=?", [agent]


def _owner(scope: str, agent: str) -> tuple[str, list[Any]]:
    """The narrower predicate that decides who owns the row behind a key."""
    if scope == "private":
        return "scope='private' AND agent=?", [agent]
    if scope == "project":
        return "scope='project' AND workspace_id=?", [config.workspace_id()]
    return "scope='public'", []


def _like(term: str) -> str:
    return (
        term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _terms(query: str) -> list[str]:
    out: list[str] = []

    def add(item: str) -> None:
        if item and item not in out:
            out.append(item)

    for chunk in _SPLIT.split((query or "").strip().lower()):
        if not chunk:
            continue
        if len(chunk) > 1:
            add(chunk)
        if len(chunk) > 2 and _CJK.search(chunk):
            for i in range(len(chunk) - 1):
                gram = chunk[i : i + 2]
                if _CJK.match(gram[0]) and _CJK.match(gram[1]):
                    add(gram)
    return out[:_MAX_TERMS]


def _row(row: Any) -> dict:
    out = dict(row)
    out["tags"] = [tag for tag in (out.get("tags") or "").split(",") if tag]
    return out


def _haystack(row: dict) -> str:
    return " ".join(
        str(row.get(field) or "") for field in ("text", "key", "tags")
    ).lower()


def remember(
    text: str,
    scope: str = "project",
    key: str = "",
    tags: Any = None,
    agent: str = "",
    run_id: str = "",
    session_id: str = "",
) -> dict:
    """Store one fact; a second write with the same key replaces the first.

    `updated_at` is bumped even when two writes land inside the same clock tick,
    so "changed since I last looked" stays answerable.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("memory text is empty")
    chosen = _normalize_scope(scope)
    agent = (agent or "").strip()
    if chosen == "private" and not agent:
        raise ValueError("private memory must belong to an agent")
    tag_text = _tags(tags)
    key = (key or "").strip()
    workspace = config.workspace_id() if chosen == "project" else ""
    stamp = db.now()

    with db.connect() as conn:
        existing = None
        if key:
            owner_sql, owner_params = _owner(chosen, agent)
            existing = conn.execute(
                f"SELECT * FROM memory WHERE key=? AND {owner_sql}",
                (key, *owner_params),
            ).fetchone()
        if existing:
            mid = existing["id"]
            updated = max(stamp, float(existing["updated_at"] or 0) + 0.001)
            conn.execute(
                "UPDATE memory SET text=?, tags=?, agent=?, updated_at=?,"
                " source_run=?, source_session=? WHERE id=?",
                (body, tag_text, agent, updated, run_id, session_id, mid),
            )
        else:
            mid = db.new_id("mem")
            conn.execute(
                "INSERT INTO memory(id, scope, key, text, tags, agent, created_at,"
                " updated_at, source_run, source_session, workspace_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    chosen,
                    key,
                    body,
                    tag_text,
                    agent,
                    stamp,
                    stamp,
                    run_id,
                    session_id,
                    workspace,
                ),
            )
    return _get(mid)


def _get(mid: str) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
    return _row(row) if row else {}


def recall(
    query: str = "",
    scope: str | None = None,
    agent: str = "",
    limit: int = 5,
) -> list[dict]:
    """Visible memories matching `query`, best match first.

    With no query the order is simply newest first. With a query, only rows
    that match at least one term are returned — a brief that says "this is
    known" while listing unrelated facts is worse than a brief with no
    memory section at all.
    """
    limit = int(limit)
    if limit <= 0:
        return []
    visible_sql, params = _visible(scope, agent)
    terms = _terms(query)
    sql = f"SELECT * FROM memory WHERE {visible_sql}"
    if terms:
        hit = (
            "(COALESCE(text,'') || ' ' || COALESCE(key,'') || ' '"
            " || COALESCE(tags,'')) LIKE ? ESCAPE '\\'"
        )
        sql += " AND (" + " OR ".join([hit] * len(terms)) + ")"
        params.extend(f"%{_like(term)}%" for term in terms)
    sql += " ORDER BY updated_at DESC, rowid DESC LIMIT 500"
    with db.connect() as conn:
        rows = [_row(row) for row in conn.execute(sql, params).fetchall()]
    if terms:
        scored = []
        for row in rows:
            hay = _haystack(row)
            score = sum(1 for term in terms if term in hay)
            if score:
                scored.append((score, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        rows = [row for _, row in scored]
    return rows[:limit]


def forget(id_or_key: str, scope: str = "", agent: str = "") -> int:
    """Delete by id or by key. Returns how many rows went away.

    The visibility predicate is the same one reads use: a run cannot delete the
    private memory of an agent it cannot read.
    """
    target = (id_or_key or "").strip()
    if not target:
        raise ValueError("forget needs an id or a key")
    visible_sql, params = _visible(scope or None, agent)
    column = "id" if target.startswith("mem-") else "key"
    with db.connect() as conn:
        cur = conn.execute(
            f"DELETE FROM memory WHERE {column}=? AND {visible_sql}",
            (target, *params),
        )
        return int(cur.rowcount or 0)


def context_for(task_text: str, agent: str = "", limit: int = 5) -> str:
    """The block a brief gets, or '' when there is nothing worth saying.

    Empty means empty: an "no memories yet" line in every prompt trains the
    worker to ignore the section.
    """
    if not (task_text or "").strip():
        return ""
    # The task text is the relevance query. Text that yields no term ("!!!")
    # carries no signal, and answering it with the newest unrelated facts is
    # exactly the prompt pollution this section must avoid.
    if not _terms(task_text):
        return ""
    rows = recall(task_text, agent=agent, limit=limit)
    if not rows:
        return ""
    lines = ["以下是这个项目已确认的事实："]
    used = len(lines[0])
    for row in rows:
        label = row["scope"] + (f"/{row['key']}" if row.get("key") else "")
        line = f"- [{label}] {_clip(row.get('text') or '')}"
        if used + len(line) + 1 > CONTEXT_BUDGET:
            break
        lines.append(line)
        used += len(line) + 1
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"
