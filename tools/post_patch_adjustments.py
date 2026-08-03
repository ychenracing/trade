#!/usr/bin/env python3
"""Small follow-up transformations kept separate from the main migration."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_GENERATED_PYTHON = (
    "account_signal_engine.py",
    "benchmark_validation.py",
    "market_data_contracts.py",
    "regime_adaptive.py",
    "test_review_fixes.py",
)


def _normalize_generated_python(path: Path) -> None:
    """Undo outer-template escape expansion before parsing generated modules."""
    text = path.read_text(encoding="utf-8")
    # The generated templates have one module-level line stripped by lstrip(),
    # while all remaining lines retain the template's eight-space margin when a
    # target ``\n`` escape was expanded too early. Remove that uniform margin.
    lines = text.splitlines(keepends=True)
    if len(lines) >= 3 and lines[2].startswith("        "):
        lines = [line[8:] if line.startswith("        ") else line for line in lines]
        text = "".join(lines)
    # Restore target-source newline escapes that the outer template interpreted.
    text = text.replace('+ "\n"', '+ "\\n"')
    text = text.replace(
        '"date,open,close,high,low,volume\n2026-01-01,1,1,1,1,10\n"',
        '"date,open,close,high,low,volume\\n2026-01-01,1,1,1,1,10\\n"',
    )
    # Keep generated code deterministic and reject hidden whitespace-only lines.
    text = "\n".join(line.rstrip() for line in text.splitlines()) + "\n"
    path.write_text(text, encoding="utf-8")


def main() -> int:
    for generated in _GENERATED_PYTHON:
        _normalize_generated_python(ROOT / generated)

    path = ROOT / "market_data_contracts.py"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(?P<indent>^[ \t]*)root = Path\(data_dir\)\n"
        r"(?P=indent)status: dict\[str, Any\] = \{\"end_date\": end_date, \"indices\": \{\}\}\n"
        r"(?P=indent)if ak is None:\n",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if match is None:
        raise RuntimeError("market refresh function structure missing")
    indent = match.group("indent")
    replacement = (
        f"{indent}root = Path(data_dir)\n"
        f"{indent}status: dict[str, Any] = {{\"end_date\": end_date, \"indices\": {{}}}}\n"
        f"{indent}# Historical replays use the frozen local snapshot. Only near-current\n"
        f"{indent}# daily scans contact the external provider, which keeps tests and\n"
        f"{indent}# reproducible past runs network-independent.\n"
        f"{indent}if pd.Timestamp(end_date).normalize() < (\n"
        f"{indent}    pd.Timestamp.today().normalize() - pd.Timedelta(days=2)\n"
        f"{indent}):\n"
        f"{indent}    for code in INDEX_SYMBOLS:\n"
        f"{indent}        existing = root / f\"{{code}}.csv\"\n"
        f"{indent}        status[\"indices\"][code] = {{\n"
        f"{indent}            \"status\": \"frozen_historical\" if existing.is_file() else \"unavailable\"\n"
        f"{indent}        }}\n"
        f"{indent}    return status\n"
        f"{indent}if ak is None:\n"
    )
    path.write_text(text[: match.start()] + replacement + text[match.end() :], encoding="utf-8")

    test_path = ROOT / "test_daily_signal_scan.py"
    tests = test_path.read_text(encoding="utf-8")
    tests, renamed = re.subn(
        r"    def test_account_mode_exits_1\(self\) -> None:\n"
        r"        \"\"\"--account must exit with code 1 \(mode is disabled\)\.\"\"\"\n",
        "    def test_missing_account_file_exits_1(self) -> None:\n"
        "        \"\"\"A missing account snapshot must fail without touching simulation state.\"\"\"\n",
        tests,
        count=1,
    )
    if renamed != 1:
        raise RuntimeError("account CLI test declaration changed upstream")
    unavailable_count = tests.count('self.assertIn("不可用", result.stdout)')
    if unavailable_count != 2:
        raise RuntimeError(
            f"account CLI assertions changed upstream: found {unavailable_count}"
        )
    tests = tests.replace(
        'self.assertIn("不可用", result.stdout)',
        'self.assertIn("Account signal scan failed", result.stdout)',
    )
    test_path.write_text(tests, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
