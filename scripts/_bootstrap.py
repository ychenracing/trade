"""Make repository-local imports work for direct script-path execution."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_project_root() -> None:
    """Prepend the repository root when Python started inside ``scripts/``."""
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
