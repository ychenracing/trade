"""Emit the lifecycle-aware AB5 trim-source research receipt on Python 3.12."""
from __future__ import annotations

from pathlib import Path
import runpy
import sys

import pytest


def test_emit_ab5_lifecycle_reallocation_receipt() -> None:
    if sys.version_info[:2] != (3, 12):
        pytest.skip("single-source AB5 lifecycle receipt emitted only on Python 3.12")
    namespace = runpy.run_path(
        str(Path(__file__).parent / "research" / "ab5_lifecycle_reallocation_probe.py")
    )
    namespace["emit_receipt"]()
