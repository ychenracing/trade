"""Download a reproducible Eastmoney forward-adjusted OHLCV snapshot."""

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
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.research_universes import (
    DEFAULT_RESEARCH_START_DATE,
    UNIVERSE_POOLS,
    symbols_for_pool,
)
from quantfusion.config.universe import SYMBOL_NAMES


DEFAULT_SYMBOLS = tuple(dict.fromkeys((*SYMBOL_NAMES, *PortfolioPolicy().regime_symbols)))
DEFAULT_RESEARCH_OUTPUT = PROJECT_ROOT / "data_cache" / "research_market"
LEGACY_START_DATE = "2024-01-01"
LEGACY_END_DATE = "2026-07-20"


def select_download_symbols(pools: Iterable[str]) -> tuple[str, ...]:
    """Return a stable pool union plus fixed regime evidence symbols."""
    pool_names = tuple(pools)
    if not pool_names:
        return DEFAULT_SYMBOLS
    ordered: list[str] = []
    for pool_name in pool_names:
        ordered.extend(symbols_for_pool(pool_name))
    ordered.extend(PortfolioPolicy().regime_symbols)
    return tuple(dict.fromkeys(ordered))


def resolve_download_window(
    start: str,
    end: str,
    *,
    research_selection: bool,
    today: str,
) -> tuple[str, str]:
    """Resolve pool research defaults without changing the retained legacy snapshot."""
    resolved_start = start or (
        DEFAULT_RESEARCH_START_DATE if research_selection else LEGACY_START_DATE
    )
    resolved_end = end or (today if research_selection else LEGACY_END_DATE)
    return resolved_start, resolved_end


def _market_id(symbol: str) -> str:
    """Return the Eastmoney market identifier for an A-share symbol."""
    return "0" if symbol.startswith(("0", "2", "3", "4", "8", "9")) else "1"


def _url(symbol: str, start: str, end: str) -> str:
    """Build the fixed Eastmoney daily forward-adjusted endpoint URL."""
    query = urllib.parse.urlencode(
        {
            "secid": f"{_market_id(symbol)}.{symbol}",
            "klt": "101",
            "fqt": "1",
            "lmt": "1000",
            "beg": start.replace("-", ""),
            "end": end.replace("-", ""),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
    )
    return f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{query}"


def _download(symbol: str, start: str, end: str) -> tuple[pd.DataFrame, str]:
    """Download one symbol with bounded retries and strict response checks."""
    errors: list[str] = []
    for attempt in range(5):
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
            # Eastmoney reports A-share volume in board lots. The strategy's ADV
            # participation control expects shares, so convert one lot to 100 shares.
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
            if attempt < 4:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{symbol} download failed: {'; '.join(errors)}")


def main() -> int:
    """Download all requested symbols and write a provenance manifest."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--start",
        default="",
        help=(
            "Snapshot start date. Research pools default to 2023-01-01; "
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
    start_date, end_date = resolve_download_window(
        args.start,
        args.end,
        research_selection=research_selection,
        today=today_str(),
    )
    output = Path(
        args.output
        or (DEFAULT_RESEARCH_OUTPUT if research_selection else MARKET_DATA_DIR)
    ).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    symbol_manifest: dict[str, object] = {}
    manifest: dict[str, object] = {
        "provider": "Eastmoney push2his",
        "adjustment": "qfq",
        "volume_unit": "shares",
        "requested_start": start_date,
        "requested_end": end_date,
        "symbols": symbol_manifest,
    }
    for symbol in symbols:
        frame, name = _download(symbol, start_date, end_date)
        path = output / f"{symbol}.csv"
        frame.assign(date=frame["date"].dt.strftime("%Y-%m-%d")).to_csv(
            path, index=False
        )
        symbol_manifest[symbol] = {
            "name": name,
            "rows": len(frame),
            "first_date": frame["date"].iloc[0].strftime("%Y-%m-%d"),
            "last_date": frame["date"].iloc[-1].strftime("%Y-%m-%d"),
        }
        print(
            f"{symbol} {name}: {len(frame)} rows, "
            f"{frame['date'].iloc[0].date()} to {frame['date'].iloc[-1].date()}"
        )
        time.sleep(0.3)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
