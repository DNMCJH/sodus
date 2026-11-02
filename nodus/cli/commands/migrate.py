"""`migrate-home`: copy the implementation-name state directory to the product name.

Phase C1 deliberately renames nothing on disk: `~/.nodus` stays the home until
somebody runs this command by hand. It is therefore cautious on purpose --
dry-run unless `--apply` is passed, one-way (the source is never moved or
deleted), verifies sqlite integrity and the task/run/artifact row counts before
it calls the copy good, and removes its own half-finished target on failure.
Rollback is `Remove-Item ~/.sodus -Recurse`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from ... import config

#: Tables whose row counts must match before the copy counts as good.
TRACKED_TABLES = ("task", "run", "artifact")

#: SQLite sidecar files. The backup API already folds the WAL into the snapshot,
#: so copying them would leave a stale journal next to a fresh database.
SKIP_SUFFIXES = ("-wal", "-shm")


def _default_source() -> Path:
    return Path(os.environ.get("NODUS_HOME") or config.DEFAULT_HOME).expanduser()


def _default_target() -> Path:
    return Path(os.environ.get("SODUS_HOME") or config.SODUS_HOME_DEFAULT).expanduser()


def _files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(SKIP_SUFFIXES)
    )


def _databases(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.sqlite3") if p.is_file())


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _size(files: list[Path]) -> str:
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return _human(total)


def _connect(path: Path, readonly: bool) -> sqlite3.Connection:
    """Open a database, read-only when asked.

    A WAL-mode database that still has its `-wal` sidecar cannot always be opened
    with `mode=ro`, so callers retry with a query-only connection instead of
    failing the whole command over a read.
    """
    if readonly:
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _inspect_with(path: Path, readonly: bool) -> dict:
    conn = _connect(path, readonly)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        rows: dict[str, int | None] = {}
        for table in TRACKED_TABLES:
            try:
                rows[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                rows[table] = None
        return {"integrity": integrity, "rows": rows}
    finally:
        conn.close()


def _inspect(path: Path) -> dict:
    for readonly in (True, False):
        try:
            return _inspect_with(path, readonly)
        except sqlite3.Error:
            if not readonly:
                raise
    raise AssertionError("unreachable")


def _rows_text(info: dict) -> str:
    parts = []
    for table in TRACKED_TABLES:
        value = info["rows"].get(table)
        parts.append(f"{table}={'absent' if value is None else value}")
    return " ".join(parts)


def _copy_db(src: Path, dst: Path) -> None:
    """Snapshot one database with the sqlite backup API (consistent, WAL-aware)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for readonly in (True, False):
        try:
            source = _connect(src, readonly)
            break
        except sqlite3.Error:
            if not readonly:
                raise
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_empty(path: Path) -> bool:
    try:
        return not any(path.iterdir())
    except OSError:
        return False


def _verify(source: Path, target: Path, files: list[Path], dbs: list[Path], before: dict) -> None:
    copied = _files(target)
    if len(copied) != len(files):
        raise RuntimeError(f"file count mismatch: source {len(files)}, target {len(copied)}")
    missing = [p for p in files if not (target / p.relative_to(source)).is_file()]
    if missing:
        raise RuntimeError(f"{len(missing)} files did not arrive, first: {missing[0]}")
    for db in dbs:
        dst = target / db.relative_to(source)
        after = _inspect(dst)
        if after["integrity"] != "ok":
            raise RuntimeError(f"{db.name}: integrity_check={after['integrity']}")
        if after["rows"] != before[db]["rows"]:
            raise RuntimeError(
                f"{db.name}: row counts changed, {before[db]['rows']} -> {after['rows']}"
            )


def migrate_home(args) -> int:
    """Print the plan, and only with `--apply` copy it and prove the copy is good."""
    source = Path(args.source).expanduser() if args.source else _default_source()
    target = Path(args.target).expanduser() if args.target else _default_target()

    if not source.is_dir():
        print(f"error: source {source} is not a directory; nothing to migrate.")
        return 1
    if _is_inside(target, source) or _is_inside(source, target):
        print("error: source and target overlap; pick a target outside the source tree.")
        return 1
    if target.exists() and not _is_empty(target):
        print(f"error: target {target} already exists and is not empty; this command never overwrites.")
        print("       move or remove it first, then re-run.")
        return 1

    files = _files(source)
    dbs = _databases(source)
    before = {db: _inspect(db) for db in dbs}

    print(f"migrate-home: {source} -> {target}")
    print(f"  source : {len(files)} files, {_size(files)}")
    for db in dbs:
        print(f"  sqlite : {db.name}  integrity={before[db]['integrity']} {_rows_text(before[db])}")
    if dbs:
        print("           (sqlite -wal/-shm are folded into the snapshot, not copied)")
    print(f"  target : {'exists (empty)' if target.exists() else 'does not exist'}")

    if not args.apply:
        print("dry-run: nothing was written. Re-run with --apply to copy and verify.")
        return 0

    try:
        target.mkdir(parents=True, exist_ok=True)
        for db in dbs:
            _copy_db(db, target / db.relative_to(source))
        for path in files:
            if path in dbs:
                continue
            dst = target / path.relative_to(source)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)
        _verify(source, target, files, dbs, before)
    except Exception as exc:  # noqa: BLE001 - any failure must clean up its own copy
        shutil.rmtree(target, ignore_errors=True)
        print(f"error: migration failed; the partial copy at {target} was deleted: {exc}")
        return 1

    print(f"done: {target} verified -- {len(files)} files, integrity ok, {len(dbs)} database(s) matching.")
    print(f"      source {source} untouched.")
    if os.environ.get("NODUS_HOME") or os.environ.get("SODUS_HOME"):
        print("      NODUS_HOME/SODUS_HOME is set, so the environment still wins; unset it to use this copy.")
    elif target == config.SODUS_HOME_DEFAULT:
        print("      sodus now reads it automatically (SODUS_HOME > NODUS_HOME > ~/.sodus > ~/.nodus).")
    else:
        print(f"      point at it with SODUS_HOME={target}")
    return 0


def add(sub) -> None:
    p = sub.add_parser(
        "migrate-home",
        help="copy ~/.nodus to ~/.sodus (dry-run by default; --apply writes)",
        description=(
            "把实现名的状态目录复制到产品名。默认只打印计划；--apply 才落地，"
            "并校验 task/run/artifact 行数与 sqlite integrity_check。"
            "不移动、不删除原目录。"
        ),
    )
    p.add_argument(
        "--from",
        dest="source",
        metavar="DIR",
        help="源目录；默认 NODUS_HOME 或 ~/.nodus",
    )
    p.add_argument(
        "--to",
        dest="target",
        metavar="DIR",
        help="目标目录；默认 SODUS_HOME 或 ~/.sodus",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="真的复制；失败会删掉复制到一半的目标目录",
    )
    p.set_defaults(func=migrate_home, needs_daemon=False)
