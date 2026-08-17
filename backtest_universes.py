#!/usr/bin/env python3
"""Compatibility CLI for :mod:`scripts.backtest_universes`."""

import argparse

from scripts.backtest_universes import *  # noqa: F403
from scripts.backtest_universes import main as _main


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(_main())
