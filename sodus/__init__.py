"""Sodus: the product name. The implementation is still the `nodus` package.

Same daemon, same database, same state directory, same MCP tools. Everything
here forwards to `nodus` so the two names stay one program instead of two
implementations that drift.
"""

from nodus import __version__

__all__ = ["__version__", "main"]


def main(argv: list[str] | None = None) -> int:
    """Run the CLI under the product name.

    Imported lazily: `import sodus` only needs the version, and the CLI pulls in
    the daemon machinery.
    """
    from nodus.cli import main as _main

    return _main(argv, prog="sodus")
