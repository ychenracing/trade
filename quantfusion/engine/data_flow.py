"""Market-data loading and causal momentum selection."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

# The same stable domain vocabulary is intentionally available to each mixin;
# responsibility is split by behavior, not by duplicating implementations.
# ruff: noqa: F401

import math
from dataclasses import replace
from typing import Any, Callable, ClassVar

import numpy as np
import pandas as pd

from quantfusion.config.engine import default_engine_config
from quantfusion.data.providers import DataFetcher
from quantfusion.domain.models import (
    AccountState,
    BarContext,
    Position,
    SectorObservation,
    Signal,
    TradeRecord,
)
from quantfusion.domain.rules import (
    SYMBOL_RE,
    floor_to_lot,
    is_finite_number,
    require_bool,
    require_finite,
    require_int,
    require_positive,
)
from quantfusion.indicators.technical import Indicators
from quantfusion.engine.configuration import EngineConfigurationMixin
from quantfusion.risk.managers import RiskManager
from quantfusion.strategy.trend import (
    ATRChannelStrategy,
    BaseStrategy,
    DualMAStrategy,
    TurtleBreakoutStrategy,
)

_SYMBOL_RE = SYMBOL_RE
_floor_to_lot = floor_to_lot
_is_finite_number = is_finite_number
_require_bool = require_bool
_require_finite = require_finite
_require_int = require_int
_require_positive = require_positive


class CoreDataFlowMixin:
    """Market-data loading and causal momentum selection."""

    def _load_market_data(
        self,
        symbols_dict: dict[str, str],
        symbol_configs: dict[str, dict],
        start_date: str,
        end_date: str,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        config_route: str,
        profile: str | None,
        data_dir: str | None,
        cache_dir: str | None,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, pd.Series]]]:
        """Load data and precompute indicators for every symbol."""
        data_map: dict[str, pd.DataFrame] = {}
        ind_map: dict[str, dict[str, pd.Series]] = {}
        for code, name in symbols_dict.items():
            print(f"  Loading data for {name} ({code})...")
            df = DataFetcher.load_stock_data(
                code,
                start_date,
                end_date,
                data_dir=data_dir,
                cache_dir=cache_dir,
            )
            df = df[(df.index >= start_ts) & (df.index <= end_ts)].copy()
            if df.empty:
                raise RuntimeError(
                    f"{code} contains no valid market data between {start_date} and {end_date}"
                )
            data_map[code] = df
            ind_map[code] = Indicators.compute_all(df, symbol_configs[code])
            route = (
                EngineConfigurationMixin._SYMBOL_PROFILE.get(
                    code, EngineConfigurationMixin.classify_symbol(code, name=name)
                )
                if config_route == "auto"
                else str(profile or "default")
            )
            if config_route == "auto" and self._uses_unmapped_auto_route(code, name):
                msg = (
                    f"  [Route warning] {name}({code}) has no explicit metadata; "
                    "using the default trend profile"
                )
                print(msg)
                if self.cfg.get("strict_unmapped", True):
                    raise RuntimeError(
                        f"strict_unmapped is enabled: {name}({code}) has no "
                        "explicit metadata or recognized name hint. Map it "
                        "explicitly or disable strict_unmapped."
                    )
            print(f"  [Parameter route] {name}({code}) -> {route}")
            print(
                f"  {name} ({code}): {len(df)} rows, "
                f"{df.index[0].date()} through {df.index[-1].date()}"
            )
        return (data_map, ind_map)

    def _select_momentum_candidates(
        self,
        data_map: dict[str, pd.DataFrame],
        symbols_dict: dict[str, str],
        date: pd.Timestamp,
    ) -> set[str]:
        """Select at most max_positions candidates by lag-safe momentum."""
        lookback = int(self.cfg.get("momentum_lookback", 20))
        scores: dict[str, float] = {}
        for code, df in data_map.items():
            if date not in df.index:
                continue
            i = df.index.get_loc(date)
            if i >= lookback:
                scores[code] = float(
                    df["close"].iloc[i] / df["close"].iloc[i - lookback] - 1
                )
        if not scores:
            return set(symbols_dict)
        max_positions = int(self.cfg.get("max_positions", 6))
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        min_slots = min(int(self.cfg.get("group_min_slots", 0)), max_positions // 2)
        selected: list[str] = []
        if min_slots > 0:
            for group in ("overseas_compute", "domestic_semiconductor"):
                group_ranked = [
                    code
                    for code, _ in ranked
                    if (
                        EngineConfigurationMixin._SYMBOL_GROUP.get(code)
                        or (
                            "domestic_semiconductor"
                            if EngineConfigurationMixin.classify_symbol(
                                code, name=symbols_dict.get(code, "")
                            )
                            == "semiconductor"
                            else "overseas_compute"
                        )
                    )
                    == group
                ]
                selected.extend(group_ranked[:min_slots])
        for code, _ in ranked:
            if code not in selected:
                selected.append(code)
            if len(selected) >= max_positions:
                break
        return set(selected[:max_positions])
