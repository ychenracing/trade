"""Standalone command-line application for deterministic backtests."""

from __future__ import annotations

import argparse

from quantfusion.application.reporting import PerformanceReport
from quantfusion.domain.rules import SYMBOL_RE
from quantfusion.engine.universe import BacktestEngine

_SYMBOL_RE = SYMBOL_RE

DEFAULT_SYMBOLS = {
    "300308": "中际旭创",
    "300502": "新易盛",
    "300394": "天孚通信",
    "688008": "澜起科技",
    "603986": "兆易创新",
}

SYMBOL_NAME_TABLE: dict[str, str] = {
    **DEFAULT_SYMBOLS,
    "688256": "寒武纪",
    "002409": "雅克科技",
    "688072": "拓荆科技",
    "688300": "联瑞新材",
    "300054": "鼎龙股份",
    "688205": "德科立",
    "920045": "蘅东光",
    "300776": "帝尔激光",
    "688535": "华海诚科",
    "688249": "晶合集成",
    "688347": "华虹宏力",
    "300666": "江丰电子",
    "600206": "有研新材",
    "688409": "富创精密",
    "688361": "中科飞测",
    "300604": "长川科技",
    "688120": "华海清科",
    "688082": "盛美上海",
}

DEFAULT_SYMBOL_NAMES = {v: k for k, v in SYMBOL_NAME_TABLE.items()}


def parse_symbols(symbols_str: str) -> dict[str, str]:
    """Resolve comma-separated stock codes or supported Chinese names."""
    result = {}
    for s in symbols_str.split(","):
        s = s.strip()
        if not s:
            continue
        if s in DEFAULT_SYMBOLS:
            result[s] = DEFAULT_SYMBOLS[s]
        elif s in DEFAULT_SYMBOL_NAMES:
            result[DEFAULT_SYMBOL_NAMES[s]] = s
        elif _SYMBOL_RE.match(s):
            result[s] = SYMBOL_NAME_TABLE.get(s, s)
        else:
            raise ValueError(
                f"Invalid stock code or name: '{s}' (use a six-digit code or a preset name)"
            )
    return result


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the standalone command-line interface."""
    parser = argparse.ArgumentParser(
        description="Quant Fusion standalone backtester"
    )
    parser.add_argument(
        "--symbol",
        "-s",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated six-digit codes or preset stock names",
    )
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--end", default="2026-07-20")
    parser.add_argument("--capital", type=float, default=2_000_000)
    parser.add_argument(
        "--data-dir",
        default="",
        help=(
            "Local forward-adjusted CSV directory. Omit this option to fetch "
            "forward-adjusted data through AKShare provider failover."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        default="",
        help=(
            "Local cache directory for incremental data fetching. On first run, "
            "fetches full history from AKShare and saves to cache. On subsequent "
            "runs, loads cached history and only fetches the latest days from "
            "AKShare, then merges and updates the cache. Combines the speed of "
            "local data with the freshness of online data."
        ),
    )
    parser.add_argument("--indicator-state", choices=["cold", "warm"], default="warm")
    parser.add_argument("--warmup-calendar-days", type=int, default=365)
    parser.add_argument("--save-dir", default="")
    parser.add_argument("--no-plot", action="store_true")
    return parser


def main() -> dict | None:
    """Run a standalone backtest from local CSV or online providers."""
    args = build_argument_parser().parse_args()
    symbols = parse_symbols(args.symbol)
    engine = BacktestEngine(args.capital)
    result = engine.run(
        symbols,
        args.start,
        args.end,
        data_dir=args.data_dir or None,
        cache_dir=args.cache_dir or None,
        indicator_state=args.indicator_state,
        warmup_calendar_days=args.warmup_calendar_days,
    )
    PerformanceReport.print_report(result, symbols)
    if args.save_dir:
        PerformanceReport.save_result(result, args.save_dir)
    if not args.no_plot:
        PerformanceReport.plot_equity_curve(
            result,
            f"equity_curve_{args.indicator_state}.png",
        )
    return result
