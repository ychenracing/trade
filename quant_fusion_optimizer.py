#!/usr/bin/env python3
"""Thin CLI and import alias for the canonical optimizer application."""

from importlib import import_module
import sys

_optimizer = import_module("quantfusion.application.optimizer")

if __name__ == "__main__":
    raise SystemExit(_optimizer.main())

sys.modules[__name__] = _optimizer
