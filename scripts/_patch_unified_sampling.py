#!/usr/bin/env python3
"""Temporary branch-only patch for 17-symbol random-subset capacity."""

from pathlib import Path


path = Path("quantfusion/application/stress_scenarios.py")
source = path.read_text(encoding="utf-8")
old = "(3, 5, 8, 12, 16)"
if source.count(old) != 2:
    raise SystemExit("expected two 22-symbol random-size tuple occurrences")
path.write_text(source.replace(old, "(3, 5, 8, 12, 15)"), encoding="utf-8")
