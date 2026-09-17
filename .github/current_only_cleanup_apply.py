from __future__ import annotations

import re
from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {text.count(old)}")
    return text.replace(old, new, 1)


def regex_once(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one regex match, found {count}")
    return updated


# Standalone backtest: one current date surface and one current default window.
path = "quantfusion/application/backtest_cli.py"
text = read(path)
text = replace_once(
    text,
    'DEFAULT_SYMBOLS = dict(list(SYMBOL_NAMES.items())[:5])\nLEGACY_BACKTEST_START_DATE = "2025-04-01"\nLEGACY_BACKTEST_END_DATE = "2026-07-20"\n',
    'DEFAULT_SYMBOLS = dict(list(SYMBOL_NAMES.items())[:5])\n',
    "backtest legacy constants",
)
text = replace_once(
    text,
    '''def resolve_backtest_window(\n    start: str,\n    end: str,\n    *,\n    pool_selected: bool,\n    today: str,\n) -> tuple[str, str]:\n    """Use 2023-to-current defaults only for configured pool research."""\n    if pool_selected:\n        return start or DEFAULT_RESEARCH_START_DATE, end or today\n    return start or LEGACY_BACKTEST_START_DATE, end or LEGACY_BACKTEST_END_DATE\n''',
    '''def resolve_backtest_window(\n    start_date: str,\n    end_date: str,\n    *,\n    today: str,\n) -> tuple[str, str]:\n    """Resolve the single current research window contract."""\n    return start_date or DEFAULT_RESEARCH_START_DATE, end_date or today\n''',
    "backtest window",
)
text = replace_once(
    text,
    '''    parser.add_argument(\n        "--start",\n        "--start-date",\n        dest="start",\n        default="",\n        help=(\n            "Backtest start date YYYY-MM-DD. Pool research defaults to "\n            f"{DEFAULT_RESEARCH_START_DATE}; legacy non-pool mode retains "\n            f"{LEGACY_BACKTEST_START_DATE}."\n        ),\n    )\n    parser.add_argument(\n        "--end",\n        "--end-date",\n        dest="end",\n        default="",\n        help=(\n            "Backtest end date YYYY-MM-DD. Pool research defaults to the current "\n            f"Shanghai-market date; legacy non-pool mode retains {LEGACY_BACKTEST_END_DATE}."\n        ),\n    )\n''',
    '''    parser.add_argument(\n        "--start-date",\n        default="",\n        help=(\n            "Backtest start date YYYY-MM-DD. Defaults to "\n            f"{DEFAULT_RESEARCH_START_DATE}."\n        ),\n    )\n    parser.add_argument(\n        "--end-date",\n        default="",\n        help="Backtest end date YYYY-MM-DD. Defaults to the current Shanghai-market date.",\n    )\n''',
    "backtest date flags",
)
text = replace_once(
    text,
    '''    start_date, end_date = resolve_backtest_window(\n        args.start,\n        args.end,\n        pool_selected=bool(args.pool),\n        today=today_str(),\n    )\n''',
    '''    start_date, end_date = resolve_backtest_window(\n        args.start_date,\n        args.end_date,\n        today=today_str(),\n    )\n''',
    "backtest main window call",
)
write(path, text)


# Data download: remove the retained Eastmoney-only branch and legacy fixed window.
path = "scripts/download_eastmoney_qfq.py"
text = read(path)
text = text.replace("import urllib.parse\nimport urllib.request\n", "")
text = replace_once(
    text,
    'DEFAULT_RESEARCH_OUTPUT = PROJECT_ROOT / "data_cache" / "research_market"\nLEGACY_START_DATE = "2024-01-01"\nLEGACY_END_DATE = "2026-07-20"\n',
    'DEFAULT_RESEARCH_OUTPUT = PROJECT_ROOT / "data_cache" / "research_market"\n',
    "download legacy constants",
)
text = replace_once(
    text,
    '''def resolve_download_window(\n    start: str,\n    end: str,\n    *,\n    research_selection: bool,\n    today: str,\n) -> tuple[str, str]:\n    """Resolve replay-window defaults without changing the retained legacy snapshot."""\n    resolved_start = start or (\n        DEFAULT_RESEARCH_START_DATE if research_selection else LEGACY_START_DATE\n    )\n    resolved_end = end or (today if research_selection else LEGACY_END_DATE)\n    return resolved_start, resolved_end\n''',
    '''def resolve_download_window(\n    start_date: str,\n    end_date: str,\n    *,\n    today: str,\n) -> tuple[str, str]:\n    """Resolve the single current market-data window."""\n    return start_date or DEFAULT_RESEARCH_START_DATE, end_date or today\n''',
    "download window",
)
text = replace_once(
    text,
    '''def research_data_start(\n    replay_start: str,\n    *,\n    research_selection: bool,\n    warmup_calendar_days: int = DEFAULT_RESEARCH_WARMUP_CALENDAR_DAYS,\n) -> str:\n    """Include causal pre-window data for warm indicators only in pool research mode."""\n    if not research_selection:\n        return replay_start\n    if warmup_calendar_days < 0:\n        raise ValueError("warmup_calendar_days must be non-negative")\n    return str(\n        (pd.Timestamp(replay_start) - pd.Timedelta(days=warmup_calendar_days)).date()\n    )\n''',
    '''def research_data_start(\n    replay_start: str,\n    *,\n    warmup_calendar_days: int = DEFAULT_RESEARCH_WARMUP_CALENDAR_DAYS,\n) -> str:\n    """Include causal pre-window data for warm indicators."""\n    if warmup_calendar_days < 0:\n        raise ValueError("warmup_calendar_days must be non-negative")\n    return str(\n        (pd.Timestamp(replay_start) - pd.Timedelta(days=warmup_calendar_days)).date()\n    )\n''',
    "download warmup",
)
text = regex_once(
    text,
    r'\ndef _market_id\(symbol: str\) -> str:.*?\n\ndef _fetch_research_symbol\(',
    '\n\ndef _fetch_symbol(',
    "remove Eastmoney-only downloader",
)
text = text.replace("_fetch_research_symbol", "_fetch_symbol")
text = text.replace("_download_research_symbols", "_download_symbols")
text = text.replace(
    '"""Replace the research/legacy manifest atomically within one output directory."""',
    '"""Replace the market-data manifest atomically within one output directory."""',
)
text = text.replace(
    '"""Build legacy-compatible and pool-aware historical data arguments."""',
    '"""Build the current historical market-data arguments."""',
)
text = replace_once(
    text,
    '''    parser.add_argument(\n        "--start",\n        "--start-date",\n        dest="start",\n        default="",\n        help=(\n            "Replay-window start date. Research pools default to 2023-01-01 and "\n            "automatically fetch one calendar year of pre-window warmup data; "\n            "legacy non-pool mode retains 2024-01-01."\n        ),\n    )\n    parser.add_argument(\n        "--end",\n        "--end-date",\n        dest="end",\n        default="",\n        help=(\n            "Snapshot end date. Research pools default to the current Shanghai-market "\n            "date; legacy non-pool mode retains 2026-07-20."\n        ),\n    )\n''',
    '''    parser.add_argument(\n        "--start-date",\n        default="",\n        help=(\n            "Replay-window start date. Defaults to 2023-01-01 and automatically "\n            "fetches one calendar year of pre-window warmup data."\n        ),\n    )\n    parser.add_argument(\n        "--end-date",\n        default="",\n        help="Snapshot end date. Defaults to the current Shanghai-market date.",\n    )\n''',
    "download date flags",
)
new_main = '''def main() -> int:\n    """Download current market data and write a fail-closed provenance manifest."""\n    args = build_argument_parser().parse_args()\n    if args.symbols:\n        symbols = tuple(args.symbols)\n    elif args.all_pools:\n        symbols = select_download_symbols(tuple(UNIVERSE_POOLS))\n    elif args.pools:\n        symbols = select_download_symbols(tuple(args.pools))\n    else:\n        symbols = DEFAULT_SYMBOLS\n    replay_start, end_date = resolve_download_window(\n        args.start_date,\n        args.end_date,\n        today=today_str(),\n    )\n    data_start = research_data_start(replay_start)\n    output = Path(args.output or DEFAULT_RESEARCH_OUTPUT).expanduser()\n    output.mkdir(parents=True, exist_ok=True)\n    symbol_manifest: dict[str, object] = {}\n    requested_symbols = list(symbols)\n    manifest: dict[str, object] = {\n        "provider": "DataFetcher failover (Eastmoney/Sina/Tencent)",\n        "adjustment": "qfq",\n        "volume_unit": "shares",\n        "requested_start": data_start,\n        "research_window_start": replay_start,\n        "warmup_calendar_days": DEFAULT_RESEARCH_WARMUP_CALENDAR_DAYS,\n        "requested_end": end_date,\n        "symbols": symbol_manifest,\n        "complete": False,\n        "requested_symbols": requested_symbols,\n        "downloaded_symbols": [],\n        "not_applicable_symbols": [],\n        "missing_symbols": requested_symbols,\n    }\n    _write_manifest(output, manifest)\n    try:\n        _download_symbols(\n            tuple(symbols),\n            start=data_start,\n            end=end_date,\n            output=output,\n            symbol_manifest=symbol_manifest,\n        )\n    except Exception as exc:\n        downloaded = [\n            code\n            for code, entry in symbol_manifest.items()\n            if isinstance(entry, dict) and entry.get("status") == "observed"\n        ]\n        not_applicable = [\n            code\n            for code, entry in symbol_manifest.items()\n            if isinstance(entry, dict)\n            and entry.get("status") == "not_applicable_pre_listing"\n        ]\n        manifest.update(\n            {\n                "downloaded_symbols": downloaded,\n                "not_applicable_symbols": not_applicable,\n                "missing_symbols": [code for code in symbols if code not in symbol_manifest],\n                "error": f"{type(exc).__name__}: {exc}",\n            }\n        )\n        _write_manifest(output, manifest)\n        raise\n    downloaded = [\n        code\n        for code, entry in symbol_manifest.items()\n        if isinstance(entry, dict) and entry.get("status") == "observed"\n    ]\n    not_applicable = [\n        code\n        for code, entry in symbol_manifest.items()\n        if isinstance(entry, dict) and entry.get("status") == "not_applicable_pre_listing"\n    ]\n    manifest.update(\n        {\n            "complete": True,\n            "downloaded_symbols": downloaded,\n            "not_applicable_symbols": not_applicable,\n            "missing_symbols": [],\n        }\n    )\n    _write_manifest(output, manifest)\n    return 0\n'''
text = regex_once(
    text,
    r'def main\(\) -> int:\n.*?\n\nif __name__ == "__main__":',
    new_main + '\n\nif __name__ == "__main__":',
    "download main",
)
write(path, text)


# Daily scan: expose one parser and remove the no-op stale compatibility flag.
path = "quantfusion/application/daily_scan.py"
text = read(path)
text = replace_once(
    text,
    'def _run_main() -> int:\n    parser = argparse.ArgumentParser(description="Daily technology-sector signal scan")\n',
    'def build_argument_parser() -> argparse.ArgumentParser:\n    """Build the current daily decision-support CLI."""\n    parser = argparse.ArgumentParser(description="Daily technology-sector signal scan")\n',
    "daily parser function",
)
text = replace_once(
    text,
    '''    parser.add_argument(\n        "--allow-stale",\n        action="store_true",\n        help="Does not override mandatory trading-session coverage or provider-stale rejection.",\n    )\n''',
    "",
    "daily allow-stale argument",
)
text = replace_once(
    text,
    '    args = parser.parse_args()\n\n    end_date = args.end_date or _today_str()\n',
    '    return parser\n\n\ndef _run_main() -> int:\n    args = build_argument_parser().parse_args()\n\n    end_date = args.end_date or _today_str()\n',
    "daily parser return",
)
text = replace_once(
    text,
    '''        if args.allow_stale:\n            print("  --allow-stale 不覆盖提供方 stale 标记或交易日完整性要求。")\n''',
    "",
    "daily allow-stale message",
)
text = text.replace(
    "    # Session coverage and the original natural-day/provider checks are\n    # independent. An override cannot turn stale inputs into trade advice.\n",
    "    # Session coverage and provider-age checks are independent and fail closed.\n",
)
write(path, text)


# Unit contracts updated to the current-only interfaces.
path = "tests/unit/test_research_universes.py"
text = read(path)
text = text.replace(
    "    LEGACY_BACKTEST_END_DATE,\n    LEGACY_BACKTEST_START_DATE,\n",
    "",
)
text = regex_once(
    text,
    r'def test_research_window_defaults_to_2023_without_mutating_legacy_backtest_defaults\(\) -> None:\n.*?\n\ndef test_pool_download_selection_includes_all_fixed_risk_evidence',
    '''def test_research_window_defaults_to_current_contract() -> None:\n    assert DEFAULT_RESEARCH_START_DATE == "2023-01-01"\n    parser = build_argument_parser()\n    args = parser.parse_args(["--pool", "pool_b", "--no-plot"])\n    assert args.pool == "pool_b"\n    assert args.start_date == ""\n    assert args.end_date == ""\n    assert resolve_backtest_window(\n        args.start_date,\n        args.end_date,\n        today="2026-09-16",\n    ) == ("2023-01-01", "2026-09-16")\n\n    explicit = parser.parse_args(\n        [\n            "--pool",\n            "pool_f",\n            "--start-date",\n            "2024-01-01",\n            "--end-date",\n            "2025-12-31",\n            "--no-plot",\n        ]\n    )\n    assert explicit.start_date == "2024-01-01"\n    assert explicit.end_date == "2025-12-31"\n\n\ndef test_download_cli_uses_current_date_flags() -> None:\n    args = download.build_argument_parser().parse_args(\n        [\n            "--pool",\n            "pool_g",\n            "--start-date",\n            "2023-01-01",\n            "--end-date",\n            "2026-09-15",\n        ]\n    )\n    assert args.start_date == "2023-01-01"\n    assert args.end_date == "2026-09-15"\n    assert args.pools == ["pool_g"]\n\n\ndef test_pool_download_selection_includes_all_fixed_risk_evidence''',
    "research current window tests",
)
text = regex_once(
    text,
    r'def test_pool_download_uses_research_window_without_mutating_legacy_defaults\(\) -> None:\n.*?\n\ndef test_research_snapshot_records_content_hash',
    '''def test_download_uses_one_current_window() -> None:\n    assert download.resolve_download_window(\n        "", "", today="2026-09-16"\n    ) == ("2023-01-01", "2026-09-16")\n    assert download.resolve_download_window(\n        "2025-04-01", "2025-12-31", today="2026-09-16"\n    ) == ("2025-04-01", "2025-12-31")\n\n\ndef test_download_includes_pre_window_warmup() -> None:\n    assert download.research_data_start(\n        "2023-01-01", warmup_calendar_days=365\n    ) == "2022-01-01"\n\n\ndef test_research_fetch_reuses_existing_provider_failover(monkeypatch) -> None:\n    index = pd.to_datetime(["2022-01-04", "2022-01-05"])\n    frame = _ohlcv_frame(index)\n    frame.attrs["volume_provider"] = "Sina"\n\n    monkeypatch.setattr(\n        download.DataFetcher,\n        "fetch_stock_data",\n        lambda symbol, start, end: frame,\n    )\n\n    actual, name, provider = download._fetch_symbol(\n        "300308", "2022-01-01", "2022-01-05"\n    )\n    assert actual is frame\n    assert name == "中际旭创"\n    assert provider == "Sina"\n\n\ndef test_research_fetch_rejects_tencent_history_truncated_at_provider_cap(monkeypatch) -> None:\n    dates = pd.bdate_range("2022-10-01", periods=1000)\n    frame = _ohlcv_frame(dates)\n    frame.attrs["volume_provider"] = "Tencent"\n    monkeypatch.setattr(\n        download.DataFetcher,\n        "fetch_stock_data",\n        lambda symbol, start, end: frame,\n    )\n    with pytest.raises(RuntimeError, match="1000-row history cap"):\n        download._fetch_symbol("300308", "2022-01-01", "2026-09-15")\n\n\ndef test_research_snapshot_records_content_hash''',
    "research downloader tests",
)
write(path, text)


# Integration tests no longer exercise a removed no-op compatibility flag.
path = "tests/integration/test_market_data_dependencies.py"
text = read(path)
text = text.replace(
    'def _scan(monkeypatch, tmp_path: Path, *, failures=None, dates=None,\n          stale=(), allow_stale=False, mode="auto"):',
    'def _scan(monkeypatch, tmp_path: Path, *, failures=None, dates=None,\n          stale=(), mode="auto"):',
)
text = text.replace('    if allow_stale:\n        argv.append("--allow-stale")\n', "")
text = text.replace('@pytest.mark.parametrize("allow_stale", [False, True])\n', "")
text = text.replace(', failure, allow_stale\n):', ', failure\n):')
text = text.replace(', allow_stale=allow_stale\n    )', '\n    )')
text = text.replace(', allow_stale\n):', '\n):')
text = text.replace('        allow_stale=allow_stale,\n', "")
text = text.replace(
    '    # The current input contract deliberately does not let --allow-stale\n    # authorize new risk from provider-stale evidence or a missing required\n    # trading-session bar. The flag remains accepted for compatibility with\n    # its narrower natural-age role, but these two conditions fail closed.\n',
    '    # Provider-stale evidence and missing required trading-session bars fail closed.\n',
)
text = text.replace(
    'def test_offline_download_default_includes_reference_only_data(',
    'def test_current_download_default_includes_reference_only_data(',
)
text = text.replace(
    '    def fake_download(code, start, end):\n        requested.append(code)\n        return _frame().reset_index(), code\n',
    '    def fake_fetch(code, start, end):\n        requested.append(code)\n        frame = _frame().reset_index().set_index("date")\n        frame.attrs["volume_provider"] = "Sina"\n        return frame, code, "Sina"\n',
)
text = text.replace('monkeypatch.setattr(download, "_download", fake_download)', 'monkeypatch.setattr(download, "_fetch_symbol", fake_fetch)')
write(path, text)

path = "tests/integration/test_daily_regime_snapshot.py"
text = read(path)
text = replace_once(
    text,
    '''@pytest.mark.parametrize("failure,allow_stale", [\n    ("error", False), ("empty", False), ("stale", False), ("lagging", False),\n    ("error", True), ("empty", True),\n])\ndef test_unusable_reference_fails_before_publication(\n    scan_inputs, failure, allow_stale, capsys\n):\n    inputs = scan_inputs\n    inputs.failures["688008"] = failure\n    if allow_stale:\n        sys.argv.append("--allow-stale")\n''',
    '''@pytest.mark.parametrize("failure", ["error", "empty", "stale", "lagging"])\ndef test_unusable_reference_fails_before_publication(\n    scan_inputs, failure, capsys\n):\n    inputs = scan_inputs\n    inputs.failures["688008"] = failure\n''',
    "daily regime stale compatibility test",
)
write(path, text)

path = "tests/integration/test_input_evidence_boundaries.py"
text = read(path)
text = replace_once(
    text,
    '''@pytest.mark.parametrize("failure", ["error", "empty"])\ndef test_allow_stale_does_not_allow_missing_trade_symbols(scan_inputs, failure):\n    scan_inputs.failures["688072"] = failure\n    sys.argv.append("--allow-stale")\n    assert dss.main() == 1\n    assert not (scan_inputs.output / "signals_2026-09-12.json").exists()\n''',
    '''@pytest.mark.parametrize("failure", ["error", "empty"])\ndef test_missing_trade_symbols_fail_closed(scan_inputs, failure):\n    scan_inputs.failures["688072"] = failure\n    assert dss.main() == 1\n    assert not (scan_inputs.output / "signals_2026-09-12.json").exists()\n''',
    "input evidence allow-stale test",
)
write(path, text)

path = "tests/integration/test_trading_day_coverage.py"
text = read(path)
text = text.replace('sys.argv.extend(["--allow-stale", "--reset-risk-state"])', 'sys.argv.append("--reset-risk-state")')
write(path, text)


# Documentation describes only the current interfaces and fail-closed behavior.
for path in ("README.md", "data/README.md", "docs/VALIDATION.md", "docs/RELEASE.md", "docs/ARCHITECTURE.md"):
    text = read(path)
    text = text.replace("或 `--allow-stale`", "")
    text = text.replace("和 `--allow-stale`", "")
    text = text.replace("与 `--allow-stale`", "")
    text = text.replace("`--allow-stale` 均不能绕过", "任何旧兼容开关都不能绕过")
    text = text.replace(
        "`backtest_cli` 支持 `--pool`，并同时接受 `--start/--end` 与 `--start-date/--end-date`；研究默认起点为 `2023-01-01`。",
        "`backtest_cli` 支持 `--pool`，日期参数统一为 `--start-date/--end-date`；默认起点为 `2023-01-01`。",
    )
    text = text.replace(
        "`backtest_cli` 同时接受旧 `--start/--end` 与新 `--start-date/--end-date` 别名。",
        "`backtest_cli` 日期参数统一为 `--start-date/--end-date`。",
    )
    text = text.replace(
        "无 `--pool` 的 `scripts.download_eastmoney_qfq.py` 保留原 Eastmoney-only 冻结快照行为和原默认窗口；显式 `--symbol` 仍只下载指定标的。Pool 研究模式",
        "`scripts.download_eastmoney_qfq.py` 统一使用当前 provider failover、2023 起默认窗口和独立运行缓存；显式 `--symbol` 仍只下载指定标的。Pool 研究模式",
    )
    text = text.replace(
        "旧无 Pool 路径继续保留原固定窗口与冻结快照语义。",
        "所有下载入口使用同一当前数据准备语义。",
    )
    write(path, text)
