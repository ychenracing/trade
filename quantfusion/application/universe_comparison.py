"""Read-only metrics and report serialization for research-universe comparisons."""

from __future__ import annotations

import csv
import json
from collections import Counter
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


def summarize_universe_result(
    pool_name: str,
    symbols: Mapping[str, str],
    start_date: str,
    end_date: str,
    result: Mapping[str, Any],
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
    gross_traded_value = sum(abs(float(getattr(trade, "gross_value", 0.0))) for trade in trades)

    positive_assets = assets > 0
    if not bool(positive_assets.all()):
        raise ValueError("equity_curve assets must stay positive for cash-ratio reporting")
    average_cash_ratio = float((cash / assets).mean())
    all_cash_day_ratio = float((position_value.abs() <= 1e-9).mean())

    max_concurrent = int(result.get("max_concurrent_symbols", 0))
    if max_concurrent < 0:
        raise ValueError("max_concurrent_symbols must be non-negative")
    concentration_proxy = 1.0 / max_concurrent if max_concurrent else 0.0

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
        # The current immutable engine result does not retain per-symbol daily
        # weights. This transparent structural proxy avoids fabricating HHI:
        # it is the reciprocal of the observed peak concurrent holdings.
        "holding_concentration_proxy": concentration_proxy,
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
        "holding_concentration_proxy",
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
        "Concentration proxy",
        "Risk events",
    )
    lines = [
        "# Universe comparison",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for record in records:
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
                    f"{float(record['holding_concentration_proxy']):.2%}",
                    str(record["risk_event_count"]),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "`holding_concentration_proxy` is `1 / max_concurrent_symbols`; the engine does not retain per-symbol daily weights, so this report does not label the proxy as HHI.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "markdown": markdown_path}
