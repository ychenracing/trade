#!/usr/bin/env python3
"""Compatibility API and CLI for :mod:`scripts.validate_basket`."""

from scripts.validate_basket import *  # noqa: F403
from scripts.validate_basket import main as _main


if __name__ == "__main__":
    raise SystemExit(_main())
