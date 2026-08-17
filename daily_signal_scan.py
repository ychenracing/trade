#!/usr/bin/env python3
"""Thin CLI and import alias for the canonical daily scan application."""

from importlib import import_module
import sys

_daily_scan = import_module("quantfusion.application.daily_scan")

if __name__ == "__main__":
    raise SystemExit(_daily_scan.main())

sys.modules[__name__] = _daily_scan
