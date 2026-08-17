#!/usr/bin/env python3
"""Compatibility CLI for :mod:`scripts.backtest_cambricon_universe`."""

import argparse

from scripts.backtest_cambricon_universe import *  # noqa: F403
from scripts.backtest_cambricon_universe import main as _main


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(_main())
