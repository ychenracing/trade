"""Temporary collection-time launcher for the isolated attribution probe."""
from __future__ import annotations

from pathlib import Path
import runpy
import sys

if sys.version_info[:2] == (3, 12):
    probe_path = Path(__file__).parent / "research" / "test_alpha_attribution_probe.py"
    namespace = runpy.run_path(str(probe_path))
    namespace["test_emit_alpha_attribution_research_receipt"]()
