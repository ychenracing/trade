"""Compare configured research pools through the production causal replay engine."""

from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path

from quantfusion.application.daily_support import today_str
from quantfusion.application.universe_comparison import (
    summarize_universe_result,
    write_universe_comparison,
)
from quantfusion.config.paths import PROJECT_ROOT
from quantfusion.config.research_universes import (
    DEFAULT_RESEARCH_START_DATE,
    UNIVERSE_POOLS,
    symbols_for_pool,
)
from quantfusion.data import contracts as market_data_contracts
from quantfusion.data.providers import DataFetcher
from quantfusion.engine.replay import ProductionReplayEngine

DEFAULT_VALIDATION_POOLS = ("pool_b", "pool_f", "pool_g")
DEFAULT_RESEARCH_DATA_DIR = PROJECT_ROOT / "data_cache" / "research_market"
DEFAULT_RESEARCH_REGIME_DIR = PROJECT_ROOT / "data_cache" / "regime"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "universe_comparison"


def _run_pool(
    pool_name: str,
    *,
    start_date: str,
    end_date: str,
    data_dir: Path,
    regime_data_dir: Path,
    initial_capital: float,
    indicator_state: str,
    warmup_calendar_days: int,
) -> dict:
    symbols = symbols_for_pool(pool_name)
    engine = ProductionReplayEngine(initial_capital)
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            symbols,
            start_date,
            end_date,
            data_dir=str(data_dir),
            regime_data_dir=str(regime_data_dir),
            indicator_state=indicator_state,
            warmup_calendar_days=warmup_calendar_days,
        )
        market_frames = {
            code: DataFetcher.load_stock_data(
                code,
                start_date,
                end_date,
                data_dir=str(data_dir),
            )
            for code in symbols
        }
    return summarize_universe_result(
        pool_name,
        symbols,
        start_date,
        end_date,
        result,
        market_frames=market_frames,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pool",
        action="append",
        choices=tuple(UNIVERSE_POOLS),
        default=None,
        help=(
            "Research pool to compare; repeat for multiple pools. "
            "Defaults to pool_b, pool_f and pool_g."
        ),
    )
    parser.add_argument(
        "--all-pools",
        action="store_true",
        help="Compare all configured pools A-J.",
    )
    parser.add_argument("--start-date", default=DEFAULT_RESEARCH_START_DATE)
    parser.add_argument(
        "--end-date",
        default="",
        help="Replay end date YYYY-MM-DD (default: current Shanghai-market date).",
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_RESEARCH_DATA_DIR))
    parser.add_argument("--regime-data-dir", default=str(DEFAULT_RESEARCH_REGIME_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--capital", type=float, default=2_000_000.0)
    parser.add_argument("--indicator-state", choices=("cold", "warm"), default="warm")
    parser.add_argument("--warmup-calendar-days", type=int, default=365)
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.all_pools and args.pool:
        parser.error("--all-pools cannot be combined with --pool")
    pools = tuple(UNIVERSE_POOLS) if args.all_pools else tuple(args.pool or DEFAULT_VALIDATION_POOLS)
    end_date = args.end_date or today_str()
    data_dir = Path(args.data_dir).expanduser()
    regime_data_dir = Path(args.regime_data_dir).expanduser()
    if not data_dir.is_dir():
        raise ValueError(
            f"Research market-data directory does not exist: {data_dir}. "
            "Run scripts.download_eastmoney_qfq with the same pool selection first."
        )
    market_data_contracts.refresh_regime_indices(
        regime_data_dir,
        end_date=end_date,
        strict=True,
    )

    rows = [
        _run_pool(
            pool_name,
            start_date=args.start_date,
            end_date=end_date,
            data_dir=data_dir,
            regime_data_dir=regime_data_dir,
            initial_capital=args.capital,
            indicator_state=args.indicator_state,
            warmup_calendar_days=args.warmup_calendar_days,
        )
        for pool_name in pools
    ]
    paths = write_universe_comparison(rows, args.output_dir)
    for row in rows:
        print(
            row["pool"],
            f"symbols={row['symbol_count']}",
            f"return={row['total_return']:.6%}",
            f"max_drawdown={row['max_drawdown']:.6%}",
            f"trades={row['total_trades']}",
            f"avg_hhi={row['holding_concentration_hhi_mean']:.6f}",
        )
    print("reports:", ", ".join(f"{name}={path}" for name, path in paths.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
