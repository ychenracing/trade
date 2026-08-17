#!/usr/bin/env python3
"""Compatibility CLI for :mod:`scripts.download_eastmoney_qfq`."""

from scripts.download_eastmoney_qfq import *  # noqa: F403
from scripts.download_eastmoney_qfq import main as _main


if __name__ == "__main__":
    raise SystemExit(_main())
