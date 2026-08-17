"""Crash-safe JSON artifact persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

def atomic_json(payload: dict[str, Any], path: Path) -> None:
    """以临时文件、刷盘和原子替换方式写入严格 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".account_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
