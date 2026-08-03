#!/usr/bin/env python3
"""Make generated Python templates raw before the one-shot migration executes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    path = ROOT / "tools" / "apply_review_fixes.py"
    text = path.read_text(encoding="utf-8")
    markers = (
        '"""Provider and fixed-index data contracts used by daily decision support."""',
        '"""Real-account decision support kept separate from the backtest state machine."""',
        '"""Reproducible simple benchmarks for strategy attribution."""',
        '"""Regression tests for the production review fixes."""',
    )
    for marker in markers:
        old = "dedent('''\n        " + marker
        new = "dedent(r'''\n        " + marker
        count = text.count(old)
        if count != 1:
            raise RuntimeError(f"expected one raw-template marker for {marker!r}, found {count}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
