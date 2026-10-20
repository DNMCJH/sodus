#!/usr/bin/env python
"""Nodus entry point. The CLI lives in `nodus/cli/`; this file only starts it."""

from __future__ import annotations

import sys

from nodus.cli import main

if __name__ == "__main__":
    sys.exit(main())
