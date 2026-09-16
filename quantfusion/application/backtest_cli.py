"""Standalone command-line application for deterministic backtests."""

from __future__ import annotations

import argparse

from quantfusion.application.daily_support import today_str
from quantfusion.application.reporting import PerformanceReport
from quantfusion.config.research_universes import (
    DEFAULT_RESEARCH_START_DATE,
    RESEARCH_SYMBOL_NAMES,
    UNIVERSE_POOLS,
    symbols_for_pool,
)
from quantfusion.config.universe import SYMBOL_NAMES
from quantfusion.domain.rules import SYMBOL_RE
from quantfusion.engine.universe import BacktestEngine

_SYMBOL_RE = SYMBOL_RE

DEFAULT_SYMBOLS = dict(list(SYMBOL_NAMES.items())[:5])

# Keep the production symbol table public contract unchanged while allowing
# research-only names to resolve through the separate research catalog.
SYMBOL_NAME_TABLE: dict[str, str] = dict(SYMBOL_NAMES)
DEFAULT_SYMBOL_NAMES = {v: k for k, v in RESEARCH_SYMBOL_NAMES.items()}


def parse_symbols(symbols_str: str) -> dict[str, str]:
    """Resolve comma-separated stock codes or supported Chinese names."""
    result = {}
    for s in symbols_str.split(","):
        s = s.strip()
        if not s:
            continue
        if s in RESEARCH_SYMBOL_NAMES:
            result[s] = RESEARCH_SYMBOL_NAMES[s]
        elif s in DEFAULT_SYMBOL_NAMES:
            result[DEFAULT_SYMBOL_NAMES[s]] = s
        elif _SYMBOL_RE.match(s):
            result[s] = RESEARCH_SYMBOL_NAMES.get(s, s)
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
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--symbol",
        "-s",
        default="",
        help="Comma-separated six-digit codes or preset stock names",
    )
    selection.add_argument(
        "--pool",
        choices=tuple(UNIVERSE_POOLS),
        default="",
        help="Configured research universe (pool_a through pool_j)",
    )
    parser.add_argument(
        "--start",
        "--start-date",
        dest="start",
        default=DEFAULT_RESEARCH_START_DATE,
        help=(
            "Backtest start date YYYY-MM-DD "
            f"(default: {DEFAULT_RESEARCH_START_DATE})"
        ),
    )
    parser.add_argument(
        "--end",
        "--end-date",
        dest="end",
        default="",
        help="Backtest end date YYYY-MM-DD (default: current Shanghai-market date)",
    )
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
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.pool:
        symbols = symbols_for_pool(args.pool)
    elif args.symbol:
        symbols = parse_symbols(args.symbol)
    else:
        symbols = dict(DEFAULT_SYMBOLS)
    end_date = args.end or today_str()
    engine = BacktestEngine(args.capital)
    result = engine.run(
        symbols,
        args.start,
        end_date,
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


if __name__ == "__main__":
    main()
