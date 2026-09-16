"""Download reproducible forward-adjusted OHLCV research and legacy snapshots."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from quantfusion.application.daily_support import today_str
from quantfusion.config.overlay import RISK_BASKET
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.research_universes import (
    DEFAULT_RESEARCH_START_DATE,
    RESEARCH_SYMBOL_NAMES,
    UNIVERSE_POOLS,
    symbols_for_pool,
)
from quantfusion.config.universe import SYMBOL_NAMES
from quantfusion.data.providers import DataFetcher


DEFAULT_SYMBOLS = tuple(dict.fromkeys((*SYMBOL_NAMES, *PortfolioPolicy().regime_symbols)))
DEFAULT_RESEARCH_OUTPUT = PROJECT_ROOT / "data_cache" / "research_market"
LEGACY_START_DATE = "2024-01-01"
LEGACY_END_DATE = "2026-07-20"
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
    start: str,
    end: str,
    *,
    research_selection: bool,
    today: str,
) -> tuple[str, str]:
    """Resolve replay-window defaults without changing the retained legacy snapshot."""
    resolved_start = start or (
        DEFAULT_RESEARCH_START_DATE if research_selection else LEGACY_START_DATE
    )
    resolved_end = end or (today if research_selection else LEGACY_END_DATE)
    return resolved_start, resolved_end


def research_data_start(
    replay_start: str,
    *,
    research_selection: bool,
    warmup_calendar_days: int = DEFAULT_RESEARCH_WARMUP_CALENDAR_DAYS,
) -> str:
    """Include causal pre-window data for warm indicators only in pool research mode."""
    if not research_selection:
        return replay_start
    if warmup_calendar_days < 0:
        raise ValueError("warmup_calendar_days must be non-negative")
    return str(
        (pd.Timestamp(replay_start) - pd.Timedelta(days=warmup_calendar_days)).date()
    )


def _market_id(symbol: str) -> str:
    """Return the Eastmoney market identifier for an A-share symbol."""
    return "0" if symbol.startswith(("0", "2", "3", "4", "8", "9")) else "1"


def _url(symbol: str, start: str, end: str) -> str:
    """Build the retained Eastmoney daily forward-adjusted endpoint URL."""
    query = urllib.parse.urlencode(
        {
            "secid": f"{_market_id(symbol)}.{symbol}",
            "klt": "101",
            "fqt": "1",
            # Long historical research can exceed the old 1000-row cap.
            "lmt": "2000",
            "beg": start.replace("-", ""),
            "end": end.replace("-", ""),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
    )
    return f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{query}"


def _download(
    symbol: str,
    start: str,
    end: str,
    *,
    attempts: int = 5,
    retry_base_delay: float = 1.5,
) -> tuple[pd.DataFrame, str]:
    """Retain the legacy Eastmoney-only snapshot path with bounded retries."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                _url(symbol, start, end),
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
                payload = json.loads(response.read().decode("utf-8"))
            data = payload.get("data")
            rows = data.get("klines", []) if isinstance(data, dict) else []
            if not rows:
                raise ValueError(f"empty kline response: {payload!r}")
            values = [row.split(",") for row in rows]
            frame = pd.DataFrame(
                values,
                columns=(
                    "date",
                    "open",
                    "close",
                    "high",
                    "low",
                    "volume_lots",
                    "amount",
                    "amplitude",
                    "change_pct",
                    "change",
                    "turnover",
                ),
            )
            for column in ("open", "close", "high", "low", "volume_lots"):
                frame[column] = pd.to_numeric(frame[column], errors="raise")
            frame["volume"] = frame.pop("volume_lots") * 100.0
            frame = frame[["date", "open", "high", "low", "close", "volume"]]
            frame["date"] = pd.to_datetime(frame["date"], errors="raise")
            frame = frame.loc[
                frame["date"].between(pd.Timestamp(start), pd.Timestamp(end))
            ].copy()
            if frame.empty or frame["date"].duplicated().any():
                raise ValueError("empty or duplicate-dated normalized response")
            if (frame[["open", "high", "low", "close"]] <= 0).any().any():
                raise ValueError("non-positive price in normalized response")
            if (frame["high"] < frame[["open", "close"]].max(axis=1)).any():
                raise ValueError("invalid high price in normalized response")
            if (frame["low"] > frame[["open", "close"]].min(axis=1)).any():
                raise ValueError("invalid low price in normalized response")
            name = str(data.get("name", ""))
            return frame, name
        except Exception as error:  # External endpoint boundary.
            errors.append(f"attempt {attempt + 1}: {error}")
            if attempt + 1 < attempts:
                time.sleep(retry_base_delay * (attempt + 1))
    raise RuntimeError(f"{symbol} download failed: {'; '.join(errors)}")


def _fetch_research_symbol(
    symbol: str, start: str, end: str
) -> tuple[pd.DataFrame, str, str]:
    """Use the existing validated Eastmoney/Sina/Tencent failover for research."""
    frame = DataFetcher.fetch_stock_data(symbol, start, end)
    provider = str(frame.attrs.get("volume_provider", "unknown"))
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
) -> None:
    """Persist one already-validated frame and its compact provenance row."""
    serial = _serializable_frame(frame)
    path = output / f"{symbol}.csv"
    serial.assign(date=serial["date"].dt.strftime("%Y-%m-%d")).to_csv(
        path, index=False
    )
    symbol_manifest[symbol] = {
        "name": name,
        "provider": provider,
        "rows": len(serial),
        "first_date": serial["date"].iloc[0].strftime("%Y-%m-%d"),
        "last_date": serial["date"].iloc[-1].strftime("%Y-%m-%d"),
    }
    print(
        f"{symbol} {name}: {provider}, {len(serial)} rows, "
        f"{serial['date'].iloc[0].date()} to {serial['date'].iloc[-1].date()}"
    )


def _download_research_symbols(
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
            try:
                frame, name, provider = _fetch_research_symbol(symbol, start, end)
            except RuntimeError as exc:
                last_error = str(exc)
                next_pending = pending[offset:]
                print(
                    f"{symbol}: all providers unavailable; deferring "
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


def main() -> int:
    """Download all requested symbols and write a provenance manifest."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--start",
        default="",
        help=(
            "Replay-window start date. Research pools default to 2023-01-01 and "
            "automatically fetch one calendar year of pre-window warmup data; "
            "legacy non-pool mode retains 2024-01-01."
        ),
    )
    parser.add_argument(
        "--end",
        default="",
        help=(
            "Snapshot end date. Research pools default to the current Shanghai-market "
            "date; legacy non-pool mode retains 2026-07-20."
        ),
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
    args = parser.parse_args()
    research_selection = bool(args.pools or args.all_pools)
    if args.symbols:
        symbols = tuple(args.symbols)
    elif args.all_pools:
        symbols = select_download_symbols(tuple(UNIVERSE_POOLS))
    elif args.pools:
        symbols = select_download_symbols(tuple(args.pools))
    else:
        symbols = DEFAULT_SYMBOLS
    replay_start, end_date = resolve_download_window(
        args.start,
        args.end,
        research_selection=research_selection,
        today=today_str(),
    )
    data_start = research_data_start(
        replay_start,
        research_selection=research_selection,
    )
    output = Path(
        args.output
        or (DEFAULT_RESEARCH_OUTPUT if research_selection else MARKET_DATA_DIR)
    ).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    symbol_manifest: dict[str, object] = {}
    manifest: dict[str, object] = {
        "provider": (
            "DataFetcher failover (Eastmoney/Sina/Tencent)"
            if research_selection
            else "Eastmoney push2his"
        ),
        "adjustment": "qfq",
        "volume_unit": "shares",
        "requested_start": data_start,
        "research_window_start": replay_start,
        "warmup_calendar_days": (
            DEFAULT_RESEARCH_WARMUP_CALENDAR_DAYS if research_selection else 0
        ),
        "requested_end": end_date,
        "symbols": symbol_manifest,
    }
    if research_selection:
        _download_research_symbols(
            tuple(symbols),
            start=data_start,
            end=end_date,
            output=output,
            symbol_manifest=symbol_manifest,
        )
    else:
        for symbol in symbols:
            frame, name = _download(symbol, data_start, end_date)
            _store_symbol_snapshot(
                output,
                symbol_manifest,
                symbol,
                frame,
                name,
                provider="Eastmoney push2his",
            )
            time.sleep(0.3)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
