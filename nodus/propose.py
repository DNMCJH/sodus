"""Proposals: parallel work that has to be merged, not just written.

The write lock made one writer per directory safe; it also made parallel work
impossible, which is the other half of "collaboration". A proposal is the way
out, and it is the same shape git already has:

    branch      a real `git worktree` per run, on branch nodus/<run-id>
    commit      the run's changes, committed inside that worktree
    PR          a `proposal` row + a patch file you can read before merging
    merge       applying the branch under the main tree's write lock

The worktree is the isolation, and it is why nothing else had to change: its
path is a different cwd, so the existing write lock already allows several
proposals to run at once, and taking the lock on the **main** tree is exactly
what "merge" means.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config, db

_TIMEOUT = 120


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_TIMEOUT,
        creationflags=config.NO_WINDOW,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"git {' '.join(args)}: {detail[-1] if detail else 'failed'}")
    return proc


def repo_root(path: Path) -> Path | None:
    """The work tree that owns this directory, or None if it is not a repo."""
    proc = _git(path, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0:
        return None
    top = (proc.stdout or "").strip()
    return Path(top) if top else None


def _base(repo: Path) -> str:
    return (_git(repo, "rev-parse", "HEAD").stdout or "").strip()


def prepare(repo: Path, run_id: str) -> dict:
    """Create the branch + worktree a propose run will work in."""
    root = repo_root(repo)
    if root is None:
        raise ValueError(
            f"{repo} is not a git repository; proposals need one "
            "(without git there is no cheap way to isolate and merge)"
        )
    branch = f"nodus/{run_id}"
    worktree = config.HOME / "worktrees" / run_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    base = _base(root)
    _git(root, "worktree", "add", "-b", branch, str(worktree), base)
    return {"repo": str(root), "branch": branch, "worktree": str(worktree), "base": base}


def collect(ctx: dict, title: str) -> dict | None:
    """Commit whatever the worker produced and turn it into a patch.

    Returns None when the run changed nothing — an empty branch is not a
    proposal, it is a no-op, and listing it would train people to ignore the
    list.

    A worker may have committed its own work before finishing; that is a clean
    tree, not an empty proposal. What decides emptiness is the diff against
    the base commit, never the dirty state of the tree.
    """
    worktree = Path(ctx["worktree"])
    if not worktree.is_dir():
        return None
    _git(worktree, "add", "-A")
    changed = _git(worktree, "status", "--porcelain").stdout or ""
    if changed.strip():
        _git(worktree, "commit", "-m", f"nodus: {title[:72] or ctx['branch']}")

    patch = _git(worktree, "diff", "--binary", f"{ctx['base']}..HEAD").stdout or ""
    if not patch.strip():
        return None
    patch_path = config.HOME / "proposals" / f"{Path(ctx['worktree']).name}.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch, encoding="utf-8")

    numstat = _git(worktree, "diff", "--numstat", f"{ctx['base']}..HEAD").stdout or ""
    files: list[dict] = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        files.append(
            {
                "path": path,
                "added": int(added) if added.isdigit() else 0,
                "removed": int(removed) if removed.isdigit() else 0,
            }
        )
    total_add = sum(f["added"] for f in files)
    total_del = sum(f["removed"] for f in files)
    return {
        "patch_path": str(patch_path),
        "files": files,
        "stat": f"{len(files)} file(s), +{total_add} −{total_del}",
    }


def record(run_id: str, task_id: str, ctx: dict, collected: dict) -> dict:
    return db.create_proposal(
        run_id=run_id,
        task_id=task_id,
        repo=ctx["repo"],
        branch=ctx["branch"],
        worktree=ctx["worktree"],
        base=ctx["base"],
        patch_path=collected["patch_path"],
        files=collected["files"],
        stat=collected["stat"],
    )


def merge(proposal: dict) -> dict:
    """Apply the branch to the main tree, under that tree's write lock."""
    repo = Path(proposal["repo"])
    owner = f"merge-{proposal['id']}"
    ok, holder = db.lock_path(repo, owner)
    if not ok:
        return {
            "ok": False,
            "reason": db.describe_busy(repo, holder),
            "conflict": False,
        }
    try:
        proc = _git(repo, "merge", "--no-ff", "--no-edit", proposal["branch"], check=False)
        if proc.returncode != 0:
            _git(repo, "merge", "--abort", check=False)
            detail = (proc.stdout or "") + (proc.stderr or "")
            conflict = bool(re.search(r"CONFLICT|Automatic merge failed", detail))
            return {
                "ok": False,
                "reason": ("merge conflict — the proposal is still open" if conflict else detail.strip()[-400:]),
                "conflict": conflict,
            }
        commit = (_git(repo, "rev-parse", "--short", "HEAD", check=False).stdout or "").strip()
    finally:
        db.release_write(owner)
    discard(proposal)
    db.close_proposal(proposal["id"], "merged")
    return {"ok": True, "commit": commit, "branch": proposal["branch"]}


def discard(proposal: dict) -> None:
    """Remove the worktree and the branch. Safe to call twice."""
    repo = Path(proposal["repo"])
    worktree = proposal.get("worktree") or ""
    if worktree and Path(worktree).exists():
        _git(repo, "worktree", "remove", "--force", worktree, check=False)
    _git(repo, "branch", "-D", proposal["branch"], check=False)
    _git(repo, "worktree", "prune", check=False)


def reject(proposal: dict) -> None:
    discard(proposal)
    db.close_proposal(proposal["id"], "rejected")


def preview(proposal: dict, limit: int = 120) -> str:
    """The patch, truncated. Enough to decide without opening a file."""
    path = Path(proposal.get("patch_path") or "")
    if not path.is_file():
        return "(patch file is gone)"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    head = lines[:limit]
    out = "\n".join(head)
    if len(lines) > limit:
        out += f"\n... ({len(lines) - limit} more lines in {path})"
    return out
