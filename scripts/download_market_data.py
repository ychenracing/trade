"""Download reproducible forward-adjusted OHLCV research and legacy snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from quantfusion.application.daily_support import today_str
from quantfusion.config.overlay import RISK_BASKET
from quantfusion.config.paths import PROJECT_ROOT
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.regime import MAX_EVIDENCE_STALENESS_DAYS
from quantfusion.config.research_universes import (
    DEFAULT_RESEARCH_START_DATE,
    RESEARCH_FIRST_TRADING_DATES,
    RESEARCH_SYMBOL_NAMES,
    UNIVERSE_POOLS,
    symbols_for_pool,
)
from quantfusion.config.universe import SYMBOL_NAMES
from quantfusion.data.providers import DataFetcher

DEFAULT_SYMBOLS = tuple(dict.fromkeys((*SYMBOL_NAMES, *PortfolioPolicy().regime_symbols)))
DEFAULT_RESEARCH_OUTPUT = PROJECT_ROOT / "data_cache" / "research_market"
DEFAULT_RESEARCH_WARMUP_CALENDAR_DAYS = 365
RESEARCH_INTER_SYMBOL_DELAY_SECONDS = 1.0
RESEARCH_COOLDOWN_SECONDS = 10.0
RESEARCH_MAX_PASSES = 3


def select_download_symbols(pools: Iterable[str]) -> tuple[str, ...]:
    """Return a stable pool union plus all fixed production risk evidence symbols."""
    pool_names = tuple(pools)
    if not pool_names:
        return DEFAULT_SYMBOLS
    ordered: list[str] = []
    for pool_name in pool_names:
        ordered.extend(symbols_for_pool(pool_name))
    ordered.extend(PortfolioPolicy().regime_symbols)
    ordered.extend(RISK_BASKET)
    return tuple(dict.fromkeys(ordered))


def resolve_download_window(
    start_date: str,
    end_date: str,
    *,
    today: str,
) -> tuple[str, str]:
    """Resolve the single current market-data window."""
    return start_date or DEFAULT_RESEARCH_START_DATE, end_date or today


def research_data_start(
    replay_start: str,
    *,
    warmup_calendar_days: int = DEFAULT_RESEARCH_WARMUP_CALENDAR_DAYS,
) -> str:
    """Include causal pre-window data for warm indicators."""
    if warmup_calendar_days < 0:
        raise ValueError("warmup_calendar_days must be non-negative")
    return str(
        (pd.Timestamp(replay_start) - pd.Timedelta(days=warmup_calendar_days)).date()
    )


def prelisting_not_applicable(symbol: str, end_date: str) -> bool:
    # Return whether the requested window ends before a known first trade.
    first_trading = RESEARCH_FIRST_TRADING_DATES.get(symbol)
    if first_trading is None:
        return False
    end = pd.Timestamp(end_date)
    if pd.isna(end):
        raise ValueError("end_date must resolve to a valid timestamp")
    return end.normalize() < pd.Timestamp(first_trading)



def _fetch_symbol(
    symbol: str, start: str, end: str
) -> tuple[pd.DataFrame, str, str]:
    """Use provider failover and reject visibly truncated/stale research history."""
    frame = DataFetcher.fetch_stock_data(symbol, start, end)
    if frame.empty:
        raise RuntimeError(f"{symbol} provider failover returned no research rows")
    provider = str(frame.attrs.get("volume_provider", "unknown"))
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    visible = frame.loc[frame.index <= end_ts]
    if visible.empty:
        raise RuntimeError(f"{symbol} provider data contains no rows through {end}")
    first = pd.Timestamp(visible.index[0])
    latest = pd.Timestamp(visible.index[-1])
    if provider == "Tencent" and len(visible) >= 1000 and first > start_ts:
        raise RuntimeError(
            f"{symbol} Tencent fallback hit the 1000-row history cap; "
            f"first={first.date()} requested_start={start_ts.date()}"
        )
    if (end_ts - latest).days > MAX_EVIDENCE_STALENESS_DAYS:
        raise RuntimeError(
            f"{symbol} provider data is stale: last={latest.date()} requested_end={end_ts.date()}"
        )
    name = RESEARCH_SYMBOL_NAMES.get(symbol, symbol)
    return frame, name, provider


def _serializable_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a stable CSV frame whether dates arrive as a column or index."""
    if "date" in frame.columns:
        out = frame.copy()
    else:
        out = frame.reset_index()
        if "date" not in out.columns:
            out.rename(columns={out.columns[0]: "date"}, inplace=True)
    out["date"] = pd.to_datetime(out["date"], errors="raise")
    return out


def _store_symbol_snapshot(
    output: Path,
    symbol_manifest: dict[str, object],
    symbol: str,
    frame: pd.DataFrame,
    name: str,
    *,
    provider: str,
    include_sha256: bool = False,
) -> None:
    """Atomically persist one validated frame and its compact provenance row."""
    serial = _serializable_frame(frame)
    path = output / f"{symbol}.csv"
    temporary = output / f".{symbol}.csv.tmp"
    serial.assign(date=serial["date"].dt.strftime("%Y-%m-%d")).to_csv(
        temporary, index=False
    )
    temporary.replace(path)
    entry: dict[str, object] = {
        "name": name,
        "status": "observed",
        "provider": provider,
        "rows": len(serial),
        "first_date": serial["date"].iloc[0].strftime("%Y-%m-%d"),
        "last_date": serial["date"].iloc[-1].strftime("%Y-%m-%d"),
    }
    if include_sha256:
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    symbol_manifest[symbol] = entry
    print(
        f"{symbol} {name}: {provider}, {len(serial)} rows, "
        f"{serial['date'].iloc[0].date()} to {serial['date'].iloc[-1].date()}"
    )


def _download_symbols(
    symbols: tuple[str, ...],
    *,
    start: str,
    end: str,
    output: Path,
    symbol_manifest: dict[str, object],
) -> None:
    """Use provider failover, preserving completed symbols across bounded passes."""
    pending = list(symbols)
    last_error = ""
    for pass_index in range(RESEARCH_MAX_PASSES):
        if not pending:
            return
        next_pending: list[str] = []
        for offset, symbol in enumerate(pending):
            if prelisting_not_applicable(symbol, end):
                first_trading = RESEARCH_FIRST_TRADING_DATES[symbol]
                symbol_manifest[symbol] = {
                    "name": RESEARCH_SYMBOL_NAMES.get(symbol, symbol),
                    "status": "not_applicable_pre_listing",
                    "provider": None,
                    "rows": 0,
                    "first_date": None,
                    "last_date": None,
                    "first_trading_date": first_trading,
                }
                print(
                    f"{symbol} {RESEARCH_SYMBOL_NAMES.get(symbol, symbol)}: "
                    f"not applicable before first trade {first_trading}"
                )
                continue
            try:
                frame, name, provider = _fetch_symbol(symbol, start, end)
            except RuntimeError as exc:
                last_error = str(exc)
                next_pending = pending[offset:]
                print(
                    f"{symbol}: research input unavailable; deferring "
                    f"{len(next_pending)} symbol(s) after pass {pass_index + 1}"
                )
                break
            _store_symbol_snapshot(
                output,
                symbol_manifest,
                symbol,
                frame,
                name,
                provider=provider,
                include_sha256=True,
            )
            time.sleep(RESEARCH_INTER_SYMBOL_DELAY_SECONDS)
        if not next_pending:
            return
        pending = next_pending
        if pass_index + 1 < RESEARCH_MAX_PASSES:
            time.sleep(RESEARCH_COOLDOWN_SECONDS)
    raise RuntimeError(
        "research snapshot download remained incomplete after bounded provider "
        f"failover; first pending symbol={pending[0] if pending else 'unknown'}; "
        f"{last_error}"
    )


def _write_manifest(output: Path, manifest: dict[str, object]) -> None:
    """Replace the market-data manifest atomically within one output directory."""
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary = output / ".manifest.json.tmp"
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(output / "manifest.json")


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the current historical market-data arguments."""
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument(
        "--start-date",
        default="",
        help=(
            "Replay-window start date. Defaults to 2023-01-01 and automatically "
            "fetches one calendar year of pre-window warmup data."
        ),
    )
    parser.add_argument(
        "--end-date",
        default="",
        help="Snapshot end date. Defaults to the current Shanghai-market date.",
    )
    parser.add_argument(
        "--output",
        default="",
        help=(
            "Output directory. Pool downloads default to data_cache/research_market; "
            "legacy non-pool downloads keep the retained data/market default."
        ),
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--symbol", action="append", dest="symbols")
    selection.add_argument(
        "--pool",
        action="append",
        dest="pools",
        choices=tuple(UNIVERSE_POOLS),
        help="Configured research pool; repeat to download a union of pools.",
    )
    selection.add_argument(
        "--all-pools",
        action="store_true",
        help="Download the union of all research pools A-J.",
    )
    return parser


def main() -> int:
    """Download current market data and write a fail-closed provenance manifest."""
    args = build_argument_parser().parse_args()
    if args.symbols:
        symbols = tuple(args.symbols)
    elif args.all_pools:
        symbols = select_download_symbols(tuple(UNIVERSE_POOLS))
    elif args.pools:
        symbols = select_download_symbols(tuple(args.pools))
    else:
        symbols = DEFAULT_SYMBOLS
    replay_start, end_date = resolve_download_window(
        args.start_date,
        args.end_date,
        today=today_str(),
    )
    data_start = research_data_start(replay_start)
    output = Path(args.output or DEFAULT_RESEARCH_OUTPUT).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    symbol_manifest: dict[str, object] = {}
    requested_symbols = list(symbols)
    manifest: dict[str, object] = {
        "provider": "DataFetcher failover (Eastmoney/Sina/Tencent)",
        "adjustment": "qfq",
        "volume_unit": "shares",
        "requested_start": data_start,
        "research_window_start": replay_start,
        "warmup_calendar_days": DEFAULT_RESEARCH_WARMUP_CALENDAR_DAYS,
        "requested_end": end_date,
        "symbols": symbol_manifest,
        "complete": False,
        "requested_symbols": requested_symbols,
        "downloaded_symbols": [],
        "not_applicable_symbols": [],
        "missing_symbols": requested_symbols,
    }
    _write_manifest(output, manifest)
    try:
        _download_symbols(
            tuple(symbols),
            start=data_start,
            end=end_date,
            output=output,
            symbol_manifest=symbol_manifest,
        )
    except Exception as exc:
        downloaded = [
            code
            for code, entry in symbol_manifest.items()
            if isinstance(entry, dict) and entry.get("status") == "observed"
        ]
        not_applicable = [
            code
            for code, entry in symbol_manifest.items()
            if isinstance(entry, dict)
            and entry.get("status") == "not_applicable_pre_listing"
        ]
        manifest.update(
            {
                "downloaded_symbols": downloaded,
                "not_applicable_symbols": not_applicable,
                "missing_symbols": [code for code in symbols if code not in symbol_manifest],
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        _write_manifest(output, manifest)
        raise
    downloaded = [
        code
        for code, entry in symbol_manifest.items()
        if isinstance(entry, dict) and entry.get("status") == "observed"
    ]
    not_applicable = [
        code
        for code, entry in symbol_manifest.items()
        if isinstance(entry, dict) and entry.get("status") == "not_applicable_pre_listing"
    ]
    manifest.update(
        {
            "complete": True,
            "downloaded_symbols": downloaded,
            "not_applicable_symbols": not_applicable,
            "missing_symbols": [],
        }
    )
    _write_manifest(output, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
