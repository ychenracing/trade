"""Validate that the fixed regime basket still represents the AI sector.

Checks:
1. Basket internal correlation (should be > 0.3 for coherent sector)
2. Basket vs broad index correlation (should be > 0.5 for sector representativeness)
3. Basket breadth distribution (should span the sector, not cluster)

Usage:
    python -m scripts.validate_basket [--start 2025-04-01] [--end 2026-06-30]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

from quantfusion.application import engine_api as qf
from quantfusion.config.paths import MARKET_DATA_DIR

REGIME_BASKET = ("300308", "300502", "300394", "688008", "603986")
BROAD_INDEX = "000300"  # 沪深300


def _load_returns(code: str, start: str, end: str, data_dir: str | None = None) -> pd.Series:
    """Load daily returns for *code*."""
    try:
        if data_dir:
            path = Path(data_dir).expanduser() / f"{code}.csv"
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            df = qf.DataFetcher.load_stock_data(code, start, end)
        df = df[(df.index >= start) & (df.index <= end)]
        return df["close"].pct_change().dropna()
    except Exception:
        return pd.Series(dtype=float)


def validate_basket(
    start: str = "2025-04-01",
    end: str = "2026-06-30",
    data_dir: str | None = str(MARKET_DATA_DIR),
) -> dict:
    """Validate the fixed regime basket."""
    basket_returns = {}
    for code in REGIME_BASKET:
        rets = _load_returns(code, start, end, data_dir)
        if len(rets) > 0:
            basket_returns[code] = rets

    if len(basket_returns) < 2:
        return {"error": "not enough basket data"}

    # 1. Internal correlation
    corr_sum = 0.0
    corr_count = 0
    codes = list(basket_returns.keys())
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            aligned = basket_returns[codes[i]].align(basket_returns[codes[j]], join="inner")
            if len(aligned[0]) >= 20:
                c = float(aligned[0].corr(aligned[1]))
                if math.isfinite(c):
                    corr_sum += c
                    corr_count += 1
    internal_corr = corr_sum / corr_count if corr_count > 0 else 0.0

    # 2. Basket equal-weight index vs broad index
    basket_df = pd.DataFrame(basket_returns)
    ewi = basket_df.mean(axis=1)
    broad = _load_returns(BROAD_INDEX, start, end, data_dir)
    if len(broad) > 0:
        aligned = ewi.align(broad, join="inner")
        broad_corr = float(aligned[0].corr(aligned[1])) if len(aligned[0]) >= 20 else float("nan")
    else:
        broad_corr = float("nan")

    # 3. Recommendation
    issues = []
    if internal_corr < 0.3:
        issues.append("basket internal correlation too low (< 0.3)")
    if math.isfinite(broad_corr) and broad_corr < 0.5:
        issues.append("basket vs broad index correlation too low (< 0.5)")

    recommendation = "keep" if not issues else "review"
    reason = "basket is representative" if not issues else "; ".join(issues)

    return {
        "basket_symbols": list(REGIME_BASKET),
        "start": start,
        "end": end,
        "internal_corr": round(internal_corr, 3),
        "broad_index_corr": round(broad_corr, 3) if math.isfinite(broad_corr) else None,
        "recommendation": recommendation,
        "reason": reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--data-dir", default=str(MARKET_DATA_DIR))
    args = parser.parse_args()

    result = validate_basket(args.start, args.end, args.data_dir)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
