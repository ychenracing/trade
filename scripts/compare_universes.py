"""Compare configured research pools through the production causal replay engine."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path

import pandas as pd

from quantfusion.application.daily_support import today_str
from quantfusion.application.universe_comparison import (
    summarize_universe_result,
    write_universe_comparison,
)
from quantfusion.config.overlay import RISK_BASKET
from quantfusion.config.paths import PROJECT_ROOT
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.regime import (
    MAX_EVIDENCE_STALENESS_DAYS,
    REGIME_INDEX_FILES,
    ROUTE_TREND_SLOW_MA,
)
from quantfusion.config.research_universes import (
    DEFAULT_RESEARCH_START_DATE,
    RESEARCH_FIRST_TRADING_DATES,
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


def required_market_symbols(pools: tuple[str, ...]) -> tuple[str, ...]:
    """Return trade-pool members plus every fixed stock-side risk input."""
    ordered: list[str] = []
    for pool_name in pools:
        ordered.extend(symbols_for_pool(pool_name))
    ordered.extend(PortfolioPolicy().regime_symbols)
    ordered.extend(RISK_BASKET)
    return tuple(dict.fromkeys(ordered))


def _manifest_payload(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"research market-data manifest is required: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid research market-data manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid research market-data manifest: expected object")
    return payload


def _latest_observation_on_or_before(path: Path, end: pd.Timestamp) -> pd.Timestamp:
    try:
        raw = pd.read_csv(path, usecols=["date"])
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid date column in research market data {path}: {exc}") from exc
    dates = pd.DatetimeIndex(pd.to_datetime(raw["date"], errors="coerce")).dropna()
    dates = dates[dates <= end]
    if dates.empty:
        raise ValueError(f"research market data has no observation through {end.date()}: {path}")
    return pd.Timestamp(dates.max())


def _manifest_entry_is_prelisting_na(
    code: str,
    entry: dict,
    *,
    end_date: str | None,
) -> bool:
    if entry.get("status") != "not_applicable_pre_listing":
        return False
    expected_first = RESEARCH_FIRST_TRADING_DATES.get(code)
    declared_first = entry.get("first_trading_date")
    if expected_first is None or declared_first != expected_first:
        raise ValueError(
            f"invalid pre-listing manifest identity for {code}: {declared_first!r}"
        )
    if end_date is None:
        raise ValueError("pre-listing manifest entries require an explicit end_date")
    end = pd.Timestamp(end_date)
    if pd.isna(end):
        raise ValueError("research market-data end_date must be valid")
    if end.normalize() >= pd.Timestamp(expected_first):
        raise ValueError(
            f"pre-listing N/A is invalid for {code}: first trade {expected_first} "
            f"is not after requested end {end.date()}"
        )
    return True


def active_symbols_for_pool(
    pool_name: str,
    data_dir: Path,
    *,
    end_date: str,
) -> dict[str, str]:
    # Return configured pool members that can exist inside this research window.
    manifest = _manifest_payload(data_dir / "manifest.json")
    if manifest.get("complete") is not True:
        raise ValueError("incomplete research market-data manifest")
    entries = manifest.get("symbols")
    if not isinstance(entries, dict):
        raise ValueError("invalid research market-data manifest: symbols must be an object")
    active: dict[str, str] = {}
    for code, name in symbols_for_pool(pool_name).items():
        entry = entries.get(code)
        if not isinstance(entry, dict):
            raise ValueError(f"research market-data manifest omits required symbol: {code}")
        if _manifest_entry_is_prelisting_na(code, entry, end_date=end_date):
            continue
        active[code] = name
    if not active:
        raise ValueError(f"{pool_name} has no listed symbols in the requested window")
    return active


def validate_market_data_directory(
    data_dir: Path,
    pools: tuple[str, ...],
    *,
    end_date: str | None = None,
) -> None:
    # Require source-attested complete research inputs before any comparison.
    if not data_dir.is_dir():
        raise ValueError(
            f"Research market-data directory does not exist: {data_dir}. "
            "Run scripts.download_eastmoney_qfq with the same pool selection first."
        )
    required = required_market_symbols(pools)
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        missing = [code for code in required if not (data_dir / f"{code}.csv").is_file()]
        if missing:
            raise ValueError(
                "missing required research market-data files: " + ", ".join(missing)
            )
    manifest = _manifest_payload(manifest_path)
    if manifest.get("complete") is not True:
        raise ValueError("incomplete research market-data manifest")
    entries = manifest.get("symbols")
    if not isinstance(entries, dict):
        raise ValueError("invalid research market-data manifest: symbols must be an object")
    absent = [code for code in required if code not in entries]
    if absent:
        raise ValueError(
            "research market-data manifest omits required symbols: " + ", ".join(absent)
        )

    attested_na: list[str] = []
    observed: list[str] = []
    for code in required:
        entry = entries.get(code)
        if not isinstance(entry, dict):
            raise ValueError(f"invalid research market-data manifest entry for {code}")
        path = data_dir / f"{code}.csv"
        if _manifest_entry_is_prelisting_na(code, entry, end_date=end_date):
            if path.exists():
                raise ValueError(
                    f"pre-listing N/A symbol must not carry a market-data CSV: {code}"
                )
            attested_na.append(code)
            continue
        if not path.is_file():
            raise ValueError(f"missing required research market-data file: {code}")
        expected_hash = entry.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError(f"research market-data manifest lacks sha256 for {code}")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"research market-data sha256 mismatch for {code}")
        observed.append(code)

    declared_na = manifest.get("not_applicable_symbols", [])
    if not isinstance(declared_na, list) or set(declared_na) != set(attested_na):
        raise ValueError("research market-data manifest pre-listing identity is inconsistent")

    if end_date is not None:
        end = pd.Timestamp(end_date)
        if pd.isna(end):
            raise ValueError("research market-data end_date must be valid")
        stale: list[str] = []
        for code in observed:
            latest = _latest_observation_on_or_before(data_dir / f"{code}.csv", end)
            if (end - latest).days > MAX_EVIDENCE_STALENESS_DAYS:
                stale.append(f"{code}:{latest.date()}")
        if stale:
            raise ValueError(
                "research market-data coverage is stale at requested end: "
                + ", ".join(stale)
            )


def validate_regime_data_directory(
    regime_data_dir: Path,
    *,
    start_date: str,
    end_date: str,
) -> dict[str, object]:
    """Require complete shared fixed-index coverage for a comparable research replay."""
    if not regime_data_dir.is_dir():
        raise ValueError(f"Research regime-data directory does not exist: {regime_data_dir}")
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    if start is pd.NaT or end is pd.NaT or start > end:
        raise ValueError("research regime window must contain valid ordered dates")

    in_window_dates: dict[str, tuple[pd.Timestamp, ...]] = {}
    warmup_counts: dict[str, int] = {}
    latest_dates: dict[str, pd.Timestamp] = {}
    for code in REGIME_INDEX_FILES.values():
        path = regime_data_dir / f"{code}.csv"
        if not path.is_file():
            raise ValueError(f"missing required regime-index file: {path}")
        try:
            frame = DataFetcher.load_stock_data(
                code,
                str((start - pd.Timedelta(days=900)).date()),
                str(end.date()),
                data_dir=str(regime_data_dir),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid regime-index evidence for {code}: {exc}") from exc
        dates = pd.DatetimeIndex(frame.index)
        dates = dates[dates <= end]
        if dates.empty or dates.has_duplicates or not dates.is_monotonic_increasing:
            raise ValueError(f"invalid regime-index date sequence for {code}")
        warmup_count = int((dates < start).sum())
        if warmup_count < ROUTE_TREND_SLOW_MA:
            raise ValueError(
                f"insufficient regime warmup for {code}: "
                f"{warmup_count} < {ROUTE_TREND_SLOW_MA} sessions"
            )
        current = tuple(pd.Timestamp(date) for date in dates[(dates >= start) & (dates <= end)])
        if not current:
            raise ValueError(f"regime-index evidence has no observations in window for {code}")
        latest = current[-1]
        if (end - latest).days > MAX_EVIDENCE_STALENESS_DAYS:
            raise ValueError(
                f"regime-index evidence is stale for {code}: "
                f"last={latest.date()} requested_end={end.date()}"
            )
        in_window_dates[code] = current
        warmup_counts[code] = warmup_count
        latest_dates[code] = latest

    date_sequences = list(in_window_dates.values())
    if any(sequence != date_sequences[0] for sequence in date_sequences[1:]):
        raise ValueError("fixed regime-index date coverage differs inside the research window")
    shared = date_sequences[0]
    return {
        "warmup_sessions": min(warmup_counts.values()),
        "observed_start_date": shared[0].strftime("%Y-%m-%d"),
        "observed_end_date": min(latest_dates.values()).strftime("%Y-%m-%d"),
        "index_codes": list(REGIME_INDEX_FILES.values()),
        "sha256": {
            code: hashlib.sha256((regime_data_dir / f"{code}.csv").read_bytes()).hexdigest()
            for code in REGIME_INDEX_FILES.values()
        },
    }


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
    configured_symbols = symbols_for_pool(pool_name)
    symbols = active_symbols_for_pool(pool_name, data_dir, end_date=end_date)
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

    expected = sorted(symbols)
    if result.get("requested_symbols") != expected:
        raise ValueError(f"{pool_name} replay changed the requested universe identity")
    if result.get("selected_symbols") != expected:
        raise ValueError(f"{pool_name} replay silently changed the selected universe")
    if result.get("unavailable_symbols") not in ([], ()):
        raise ValueError(f"{pool_name} replay reported unavailable symbols")

    row = summarize_universe_result(
        pool_name,
        configured_symbols,
        start_date,
        end_date,
        result,
        market_frames=market_frames,
        active_symbols=symbols,
    )
    requested_start = pd.Timestamp(start_date)
    requested_end = pd.Timestamp(end_date)
    observed_start = pd.Timestamp(row["observed_start_date"])
    observed_end = pd.Timestamp(row["observed_end_date"])
    if (observed_start - requested_start).days > MAX_EVIDENCE_STALENESS_DAYS:
        raise ValueError(
            f"{pool_name} observed replay begins too late: "
            f"{observed_start.date()} vs requested {requested_start.date()}"
        )
    if (requested_end - observed_end).days > MAX_EVIDENCE_STALENESS_DAYS:
        raise ValueError(
            f"{pool_name} observed replay ends too early: "
            f"{observed_end.date()} vs requested {requested_end.date()}"
        )
    return row


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
        help=(
            "Requested replay end date YYYY-MM-DD. Defaults to the current "
            "Shanghai-market date; reports separately disclose actual observed coverage."
        ),
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
    validate_market_data_directory(data_dir, pools, end_date=end_date)
    market_data_contracts.refresh_regime_indices(
        regime_data_dir,
        end_date=end_date,
        strict=True,
        allow_provider_fallback=True,
    )
    regime_coverage = validate_regime_data_directory(
        regime_data_dir,
        start_date=args.start_date,
        end_date=end_date,
    )
    manifest_path = data_dir / "manifest.json"
    market_manifest_sha256 = (
        hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if manifest_path.is_file()
        else None
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
    for row in rows:
        row["market_manifest_sha256"] = market_manifest_sha256
        row["regime_evidence"] = dict(regime_coverage)
    paths = write_universe_comparison(rows, args.output_dir)
    for row in rows:
        print(
            row["pool"],
            f"symbols={row['symbol_count']}",
            f"observed={row['observed_start_date']}..{row['observed_end_date']}",
            f"return={row['total_return']:.6%}",
            f"max_drawdown={row['max_drawdown']:.6%}",
            f"trades={row['total_trades']}",
            f"avg_hhi={row['holding_concentration_hhi_mean']:.6f}",
        )
    print("reports:", ", ".join(f"{name}={path}" for name, path in paths.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
