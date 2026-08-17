#!/usr/bin/env python3
"""Compatibility CLI for :mod:`scripts.run_regime_validation`."""

from scripts.run_regime_validation import *  # noqa: F403
from scripts.run_regime_validation import main as _main


if __name__ == "__main__":
    raise SystemExit(_main())
