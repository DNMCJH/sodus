# budget

A trimmed copy of `nodus/budget.py` from this repository, plus the two seams it
reads through (`nodus/config.py`, `nodus/db.py`). The daily ceiling is the real
policy: CNY only, today only, refuse new runs at the limit.

The workspace database is an SQLite file at `NODUS_DB` (default `nodus.db`),
with the one `run` table the budget reads.

```
python -m pytest -q
```
