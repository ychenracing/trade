"""Temporary postprocessor for the 1.0.1 research closure staging run."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

report = ROOT / "quantfusion/application/universe_comparison.py"
text = report.read_text(encoding="utf-8")
replacements = {
    '        json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n",':
        '        json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\\n",',
    '    markdown_path.write_text("\n".join(lines), encoding="utf-8")':
        '    markdown_path.write_text("\\n".join(lines), encoding="utf-8")',
}
for old, new in replacements.items():
    if text.count(old) != 1:
        raise RuntimeError(f"unexpected generated report literal count: {old!r}")
    text = text.replace(old, new)
report.write_text(text, encoding="utf-8")

comparison = ROOT / "scripts/compare_universes.py"
text = comparison.read_text(encoding="utf-8")
old = '''    required = required_market_symbols(pools)
    manifest = _manifest_payload(data_dir / "manifest.json")
'''
new = '''    required = required_market_symbols(pools)
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        missing = [code for code in required if not (data_dir / f"{code}.csv").is_file()]
        if missing:
            raise ValueError(
                "missing required research market-data files: " + ", ".join(missing)
            )
    manifest = _manifest_payload(manifest_path)
'''
if text.count(old) != 1:
    raise RuntimeError("unexpected generated manifest-validation block")
comparison.write_text(text.replace(old, new), encoding="utf-8")

print("generated closure source postprocessed")
