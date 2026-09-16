"""Run the temporary winner-lifecycle receipt early on Python 3.12 CI."""
from __future__ import annotations

from pathlib import Path
import runpy
import sys

import pytest


def test_emit_winner_lifecycle_receipt() -> None:
    if sys.version_info[:2] != (3, 12):
        pytest.skip("single-source research receipt emitted only on Python 3.12")
    namespace = runpy.run_path(
        str(Path(__file__).parent / "research" / "winner_lifecycle_probe.py")
    )
    namespace["emit_receipt"]()
