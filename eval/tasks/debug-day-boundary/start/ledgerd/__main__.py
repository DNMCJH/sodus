"""`python -m ledgerd` == `python -m ledgerd.cli`."""

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
