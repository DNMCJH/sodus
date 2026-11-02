"""The `sodus` console script: forwards to `nodus.cli:main`.

Only the usage line differs; the parser, the commands, the daemon and the
database are the ones in `nodus`.
"""

from __future__ import annotations

from nodus.cli import main as _main


def main(argv: list[str] | None = None) -> int:
    return _main(argv, prog="sodus")


__all__ = ["main"]
