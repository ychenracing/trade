#!/usr/bin/env python3
"""Temporary branch-only patch for the current five-symbol reference core."""

from pathlib import Path


path = Path("quantfusion/config/portfolio.py")
text = path.read_text(encoding="utf-8")
import_anchor = "from quantfusion.domain.rules import (\n"
if text.count(import_anchor) != 1:
    raise SystemExit("expected one portfolio domain-rules import anchor")
text = text.replace(
    import_anchor,
    "from quantfusion.config.universe import VALIDATION_UNIVERSES\n"
    + import_anchor,
    1,
)
old = '''    regime_symbols: tuple[str, ...] = (
        "300308",
        "300502",
        "300394",
        "688008",
        "603986",
    )'''
new = '    regime_symbols: tuple[str, ...] = VALIDATION_UNIVERSES["5_symbols"]'
if text.count(old) != 1:
    raise SystemExit("expected one legacy five-symbol reference core")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
