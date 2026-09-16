"""Read-only metrics and report serialization for research-universe comparisons."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd


def _equity_frame(result: Mapping[str, Any]) -> pd.DataFrame:
    equity = result.get("equity_curve")
    if not isinstance(equity, pd.DataFrame) or equity.empty:
        raise ValueError("comparison result requires a non-empty equity_curve DataFrame")
    required = {"assets", "cash", "position_value"}
    missing = sorted(required - set(equity.columns))
    if missing:
        raise ValueError(f"equity_curve is missing audit fields: {missing}")
    return equity


def _latest_close_on_or_before(frame: pd.DataFrame, date: pd.Timestamp) -> float:
    """Mirror the engine's causal close-marking rule without using future prices."""
    history = frame.loc[frame.index <= pd.Timestamp(date), "close"]
    closes = pd.to_numeric(history, errors="coerce")
    closes = closes[closes.notna() & (closes > 0)]
    if closes.empty:
        return 0.0
    return float(closes.iloc[-1])


def _holding_concentration_hhi(
    trades: list[Any],
    dates: pd.DatetimeIndex,
    market_frames: Mapping[str, pd.DataFrame],
) -> tuple[float, float]:
    """Rebuild causally close-marked symbol weights and return mean/max HHI."""
    trades_by_date: dict[pd.Timestamp, list[Any]] = defaultdict(list)
    for trade in trades:
        trade_date = pd.Timestamp(getattr(trade, "date"))
        trades_by_date[trade_date].append(trade)

    shares: dict[str, int] = defaultdict(int)
    observed: list[float] = []
    for date in pd.DatetimeIndex(dates):
        for trade in trades_by_date.get(pd.Timestamp(date), []):
            symbol = str(getattr(trade, "symbol"))
            quantity = int(getattr(trade, "shares"))
            direction = str(getattr(trade, "direction"))
            if direction == "buy":
                shares[symbol] += quantity
            elif direction == "sell":
                shares[symbol] -= quantity
            else:
                raise ValueError(f"unknown trade direction in comparison report: {direction}")
            if shares[symbol] < 0:
                raise ValueError(f"negative reconstructed holding for {symbol}")

        values: list[float] = []
        for symbol, quantity in shares.items():
            if quantity <= 0:
                continue
            frame = market_frames.get(symbol)
            if not isinstance(frame, pd.DataFrame) or "close" not in frame.columns:
                raise ValueError(f"missing close-price frame for held symbol {symbol}")
            close = _latest_close_on_or_before(frame, pd.Timestamp(date))
            if close <= 0:
                raise ValueError(
                    f"missing causal close price for held symbol {symbol} on or before "
                    f"{pd.Timestamp(date).date()}"
                )
            values.append(quantity * close)
        total = sum(values)
        if total > 0:
            observed.append(sum((value / total) ** 2 for value in values))

    if not observed:
        return 0.0, 0.0
    return float(sum(observed) / len(observed)), float(max(observed))


def _trade_gross_value(trade: Any) -> float:
    """Use the audited gross value, with a deterministic compatibility fallback."""
    gross = float(getattr(trade, "gross_value", 0.0))
    if gross > 0:
        return gross
    shares = int(getattr(trade, "shares", 0))
    price = float(getattr(trade, "price", 0.0))
    return max(shares, 0) * max(price, 0.0)


def summarize_universe_result(
    pool_name: str,
    symbols: Mapping[str, str],
    start_date: str,
    end_date: str,
    result: Mapping[str, Any],
    *,
    market_frames: Mapping[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """Derive comparable research metrics without changing engine decisions."""
    equity = _equity_frame(result)
    assets = equity["assets"].astype(float)
    cash = equity["cash"].astype(float)
    position_value = equity["position_value"].astype(float)
    average_assets = float(assets.mean())
    if average_assets <= 0:
        raise ValueError("equity_curve average assets must be positive")

    trades = result.get("trades")
    if not isinstance(trades, list):
        raise ValueError("comparison result requires a trades list")
    gross_traded_value = sum(abs(_trade_gross_value(trade)) for trade in trades)

    positive_assets = assets > 0
    if not bool(positive_assets.all()):
        raise ValueError("equity_curve assets must stay positive for cash-ratio reporting")
    average_cash_ratio = float((cash / assets).mean())
    all_cash_day_ratio = float((position_value.abs() <= 1e-9).mean())

    max_concurrent = int(result.get("max_concurrent_symbols", 0))
    if max_concurrent < 0:
        raise ValueError("max_concurrent_symbols must be non-negative")
    if market_frames is None:
        hhi_mean: float | None = None
        hhi_max: float | None = None
    else:
        hhi_mean, hhi_max = _holding_concentration_hhi(
            trades,
            pd.DatetimeIndex(equity.index),
            market_frames,
        )

    risk_events = result.get("risk_events")
    if not isinstance(risk_events, list):
        raise ValueError("comparison result requires a risk_events list")
    event_types = Counter(
        str(item.get("event", "unknown"))
        for item in risk_events
        if isinstance(item, Mapping)
    )

    return {
        "pool": pool_name,
        "symbol_count": len(symbols),
        "symbols": list(symbols),
        "start_date": start_date,
        "end_date": end_date,
        "total_return": float(result["total_return"]),
        "annual_return": float(result["annual_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "total_trades": int(result["total_trades"]),
        "turnover_ratio": gross_traded_value / average_assets,
        "all_cash_day_ratio": all_cash_day_ratio,
        "average_cash_ratio": average_cash_ratio,
        "holding_concentration_hhi_mean": hhi_mean,
        "holding_concentration_hhi_max": hhi_max,
        "max_concurrent_symbols": max_concurrent,
        "risk_event_count": len(risk_events),
        "risk_event_types": dict(sorted(event_types.items())),
    }


def write_universe_comparison(
    rows: Iterable[Mapping[str, Any]], output_dir: str | Path
) -> dict[str, Path]:
    """Write JSON, CSV and human-readable Markdown research reports."""
    records = [dict(row) for row in rows]
    if not records:
        raise ValueError("universe comparison requires at least one result")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    json_path = output / "comparison.json"
    json_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    scalar_fields = (
        "pool",
        "symbol_count",
        "start_date",
        "end_date",
        "total_return",
        "annual_return",
        "max_drawdown",
        "total_trades",
        "turnover_ratio",
        "all_cash_day_ratio",
        "average_cash_ratio",
        "holding_concentration_hhi_mean",
        "holding_concentration_hhi_max",
        "max_concurrent_symbols",
        "risk_event_count",
    )
    csv_path = output / "comparison.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=scalar_fields)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record[field] for field in scalar_fields})

    markdown_path = output / "comparison.md"
    headers = (
        "Pool",
        "Stocks",
        "Window",
        "Return",
        "Max DD",
        "Annual",
        "Trades",
        "Turnover",
        "Cash days",
        "Avg cash",
        "Avg HHI",
        "Peak HHI",
        "Risk events",
    )
    lines = [
        "# Universe comparison",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for record in records:
        hhi_mean = record["holding_concentration_hhi_mean"]
        hhi_max = record["holding_concentration_hhi_max"]
        lines.append(
            "| "
            + " | ".join(
                (
                    str(record["pool"]),
                    str(record["symbol_count"]),
                    f"{record['start_date']} → {record['end_date']}",
                    f"{float(record['total_return']):.2%}",
                    f"{float(record['max_drawdown']):.2%}",
                    f"{float(record['annual_return']):.2%}",
                    str(record["total_trades"]),
                    f"{float(record['turnover_ratio']):.2f}",
                    f"{float(record['all_cash_day_ratio']):.2%}",
                    f"{float(record['average_cash_ratio']):.2%}",
                    "N/A" if hhi_mean is None else f"{float(hhi_mean):.2%}",
                    "N/A" if hhi_max is None else f"{float(hhi_max):.2%}",
                    str(record["risk_event_count"]),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "HHI is reconstructed from executed fills and the latest closing price known by each portfolio date; cash-only days are reported separately and excluded from the HHI average.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "markdown": markdown_path}
