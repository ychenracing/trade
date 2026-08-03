#!/usr/bin/env python3
"""Small follow-up transformations kept separate from the main migration."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_all(path: str, old: str, new: str, expected: int) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{path}: expected {expected} matches, found {count}")
    target.write_text(text.replace(old, new), encoding="utf-8")


def main() -> int:
    path = ROOT / "market_data_contracts.py"
    text = path.read_text(encoding="utf-8")
    marker = '''    root = Path(data_dir)\n    status: dict[str, Any] = {"end_date": end_date, "indices": {}}\n    if ak is None:\n'''
    replacement = '''    root = Path(data_dir)\n    status: dict[str, Any] = {"end_date": end_date, "indices": {}}\n    # Historical replays use the frozen local snapshot. Only near-current\n    # daily scans contact the external provider, which keeps tests and\n    # reproducible past runs network-independent.\n    if pd.Timestamp(end_date).normalize() < (\n        pd.Timestamp.today().normalize() - pd.Timedelta(days=2)\n    ):\n        for code in INDEX_SYMBOLS:\n            existing = root / f"{code}.csv"\n            status["indices"][code] = {\n                "status": "frozen_historical" if existing.is_file() else "unavailable"\n            }\n        return status\n    if ak is None:\n'''
    if text.count(marker) != 1:
        raise RuntimeError("market refresh marker missing")
    path.write_text(text.replace(marker, replacement, 1), encoding="utf-8")

    test_path = ROOT / "test_daily_signal_scan.py"
    tests = test_path.read_text(encoding="utf-8")
    tests = tests.replace(
        '    def test_account_mode_exits_1(self) -> None:\n        """--account must exit with code 1 (mode is disabled)."""\n',
        '    def test_missing_account_file_exits_1(self) -> None:\n        """A missing account snapshot must fail without touching simulation state."""\n',
        1,
    )
    if tests.count('self.assertIn("不可用", result.stdout)') != 2:
        raise RuntimeError("account CLI assertions changed upstream")
    tests = tests.replace(
        'self.assertIn("不可用", result.stdout)',
        'self.assertIn("Account signal scan failed", result.stdout)',
    )
    test_path.write_text(tests, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
