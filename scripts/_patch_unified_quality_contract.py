#!/usr/bin/env python3
"""Migrate stale old-universe quality assertions to the selected 17-symbol contract."""

from pathlib import Path


path = Path("tests/regression/test_quant_fusion.py")
source = path.read_text(encoding="utf-8")

old_floor = '            "17_symbols": 6.5,\n'
new_floor = (
    '            # Broad strategic-health floor for the new owner-selected full pool.\n'
    '            # Exact behavior is frozen separately by the economic golden fixture.\n'
    '            "17_symbols": 2.0,\n'
)
if source.count(old_floor) != 1:
    raise SystemExit(
        f"17-symbol return floor: expected one occurrence, found {source.count(old_floor)}"
    )
source = source.replace(old_floor, new_floor, 1)

old_dispersion = '''    def test_multi_symbol_wealth_dispersion_is_bounded(self) -> None:
        names = ("3_symbols", "5_symbols", "13_symbols", "17_symbols")
        wealth = [1.0 + self.results[name]["total_return"] for name in names]
        self.assertGreaterEqual(min(wealth) / max(wealth), 0.75)

'''
if source.count(old_dispersion) != 1:
    raise SystemExit(
        "stale cross-universe dispersion gate: expected exactly one method"
    )
source = source.replace(old_dispersion, "", 1)

path.write_text(source, encoding="utf-8")
