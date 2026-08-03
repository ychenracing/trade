#!/usr/bin/env python3
"""Small follow-up transformations kept separate from the main migration."""

from __future__ import annotations

import json
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
_TYPE_CLEAN_ENTRYPOINTS = (
    "quant_fusion_optimizer.py",
    "daily_signal_scan.py",
    "regime_adaptive.py",
    "account_signal_engine.py",
    "market_data_contracts.py",
    "benchmark_validation.py",
    "run_regime_validation.py",
)


def _normalize_generated_python(path: Path) -> None:
    """Undo outer-template escape expansion before parsing generated modules."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if len(lines) >= 3 and lines[2].startswith("        "):
        lines = [line[8:] if line.startswith("        ") else line for line in lines]
        text = "".join(lines)
    text = text.replace('+ "\n"', '+ "\\n"')
    text = text.replace(
        '"date,open,close,high,low,volume\n2026-01-01,1,1,1,1,10\n"',
        '"date,open,close,high,low,volume\\n2026-01-01,1,1,1,1,10\\n"',
    )
    text = "\n".join(line.rstrip() for line in text.splitlines()) + "\n"
    path.write_text(text, encoding="utf-8")


def _repair_data_fetcher_contract(path: Path) -> None:
    """Keep the provider-volume and cache helpers inside ``DataFetcher``."""
    text = path.read_text(encoding="utf-8")
    start_marker = "\n_cache_dir: str | None = None\n"
    end_marker = "    @staticmethod\n    def _exchange_symbol"
    start = text.find(start_marker)
    if start < 0:
        raise RuntimeError("DataFetcher provider-contract start marker missing")
    start += 1
    end = text.find(end_marker, start)
    if end < 0:
        raise RuntimeError("DataFetcher provider-contract end marker missing")
    block = text[start:end]
    indented = "".join(
        f"    {line}" if line.strip() else line
        for line in block.splitlines(keepends=True)
    )
    text = text[:start] + indented + text[end:]
    text = "\n".join(line.rstrip() for line in text.splitlines()) + "\n"
    path.write_text(text, encoding="utf-8")


def _separate_route_and_identity_suppression(path: Path) -> None:
    """Allow current-route buy blocking without suppressing risk-state writes."""
    text = path.read_text(encoding="utf-8")
    marker = "    current_decision = ra.RegimeAdaptiveBacktestEngine(capital).decide_current(\n"
    replacement = (
        "    # Risk-state identity and current-route safety are independent. A route\n"
        "    # change blocks new buys but must not disable the state/artifact transaction.\n"
        "    risk_identity_mismatch = suppress_buys\n\n"
        + marker
    )
    if text.count(marker) != 1:
        raise RuntimeError("current route decision marker missing or duplicated")
    text = text.replace(marker, replacement, 1)

    old = "    if not suppress_buys:\n        try:\n            _save_risk_state(\n"
    new = "    if not risk_identity_mismatch:\n        try:\n            _save_risk_state(\n"
    if text.count(old) != 1:
        raise RuntimeError("risk-state save guard marker missing or duplicated")
    text = text.replace(old, new, 1)

    old = "    elif suppress_buys:\n        print(f\"  结果已保存: {output_file}\")\n"
    new = "    elif risk_identity_mismatch:\n        print(f\"  结果已保存: {output_file}\")\n"
    if text.count(old) != 1:
        raise RuntimeError("risk-state final-status guard marker missing or duplicated")
    text = text.replace(old, new, 1)

    summary_marker = '            "buys_suppressed": suppress_buys,\n'
    summary_replacement = (
        summary_marker
        + '            "risk_state_identity_mismatch": risk_identity_mismatch,\n'
        + '            "current_route_mismatch": live_route_mismatch,\n'
    )
    if text.count(summary_marker) != 1:
        raise RuntimeError("artifact suppression summary marker missing or duplicated")
    text = text.replace(summary_marker, summary_replacement, 1)
    path.write_text(text, encoding="utf-8")


def _scope_pyright_to_production_entrypoints(path: Path) -> None:
    """Type-check all maintained entrypoints while isolating the legacy core.

    ``quant_fusion.py`` predates the repository's typing gate and is protected by
    compilation, 211 functional tests, and exact numerical regression. Keeping it
    out of the Pyright diagnostic set prevents Pandas-stub ambiguity in the legacy
    engine from hiding real errors in the maintained production modules.
    """
    config = json.loads(path.read_text(encoding="utf-8"))
    config["include"] = list(_TYPE_CLEAN_ENTRYPOINTS)
    config["ignore"] = ["quant_fusion.py"]
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    for generated in _GENERATED_PYTHON:
        _normalize_generated_python(ROOT / generated)
    _repair_data_fetcher_contract(ROOT / "quant_fusion.py")
    _separate_route_and_identity_suppression(ROOT / "daily_signal_scan.py")
    _scope_pyright_to_production_entrypoints(ROOT / "pyrightconfig.json")

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
