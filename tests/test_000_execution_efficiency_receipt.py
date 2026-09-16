"""Emit the temporary Phase-3 execution-efficiency receipt on Python 3.12."""
from __future__ import annotations

from pathlib import Path
import runpy
import sys

import pytest


def test_emit_execution_efficiency_receipt() -> None:
    if sys.version_info[:2] != (3, 12):
        pytest.skip("single-source execution-efficiency receipt emitted only on Python 3.12")
    namespace = runpy.run_path(
        str(Path(__file__).parent / "research" / "execution_efficiency_probe.py")
    )
    namespace["emit_receipt"]()
