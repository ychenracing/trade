#!/usr/bin/env python3
"""Compatibility CLI for the canonical stress application."""

from __future__ import annotations

if __name__ == "__main__":
    from quantfusion.application.stress import main

    raise SystemExit(main())
else:
    import sys
    from quantfusion.application import stress as _implementation

    sys.modules[__name__] = _implementation
