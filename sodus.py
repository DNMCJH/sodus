#!/usr/bin/env python
"""Sodus entry point. The CLI lives in sodus/cli/; this file only starts it."""

from __future__ import annotations

import sys

from sodus.cli import main

if __name__ == "__main__":
    sys.exit(main())
