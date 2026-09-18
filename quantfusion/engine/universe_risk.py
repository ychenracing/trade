"""Universe-aware sector and portfolio risk result decoration."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false


from pathlib import Path

import numpy as np
import pandas as pd

from quantfusion.config.paths import REGIME_DATA_DIR
from quantfusion.config.regime import REGIME_INDEX_FILES
from quantfusion.data.providers import DataFetcher
from quantfusion.domain.models import SectorObservation, Signal
from quantfusion.risk.managers import RecoverableDrawdownRiskManager
from quantfusion.strategy.trend import BaseStrategy

# Experiment C — index-based sector guard.
# Market-regime state machine keeps using policy.regime_symbols unchanged.
INDEX_MODE_TECH_ONLY = "tech_only"
INDEX_MODE_DUAL_CONFIRM = "dual_confirm"
VALID_INDEX_MODES = frozenset({INDEX_MODE_TECH_ONLY, INDEX_MODE_DUAL_CONFIRM})

GUARD_SCOPE_BY_MODE = {
    INDEX_MODE_TECH_ONLY: "index_tech_only_000682",
    INDEX_MODE_DUAL_CONFIRM: "index_dual_confirm_000300_000682",
    None: "fixed_signal_only_regime_basket",
}

# Mapping (also written to artifacts):
# tech_only:
#   equal_return := 000682 daily return
#   shock_breadth / recovery_breadth := binary MA membership of 000682
#   (1.0 above MA, 0.0 below). Multi-name breadth is N/A; existing thresholds
#   (shock_breadth <= 0.2 / recovery_breadth >= 0.8) become below/above MA.
# dual_confirm:
#   shock requires EACH of 000300 and 000682 to independently meet
#   (return <= sector_shock_return AND close <= shock MA)
#   recovery requires EACH to independently meet
#   (return > 0 AND close > recovery MA AND its own short normalized path
#   is above its recovery MA). Stricter; fewer false clears.


class UniverseRiskMixin:
    """Universe-aware sector and portfolio risk result decoration."""

    def _sector_guard_index_mode(self) -> str | None:
        """Return active index-guard probe mode, or None for the stock basket."""
        raw = self.cfg.get("sector_guard_index_mode", INDEX_MODE_TECH_ONLY)
        if raw in (None, "", "off", "regime_basket"):
            return None
        mode = str(raw)
        if mode not in VALID_INDEX_MODES:
            raise ValueError(
                "sector_guard_index_mode must be one of "
                f"{sorted(VALID_INDEX_MODES) + ['off', 'regime_basket']}; got {mode!r}"
            )
        return mode

    def _sector_guard_scope_mode(self) -> str:
        """Audit label for which observation set drives the sector guard."""
        return GUARD_SCOPE_BY_MODE[self._sector_guard_index_mode()]

    def _ensure_sector_guard_index_frames(self) -> dict[str, pd.DataFrame]:
        """Load 000300/000682 from regime dirs; fail closed if anything is missing."""
        cached = getattr(self, "_sector_guard_index_frames", None)
        if cached is not None:
            return cached
        data_dir = Path(
            str(self.cfg.get("sector_guard_index_data_dir") or REGIME_DATA_DIR)
        ).expanduser()
        if not data_dir.is_dir():
            raise RuntimeError(
                f"sector-guard index data directory missing (fail closed): {data_dir}"
            )
        frames: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        for code in REGIME_INDEX_FILES.values():
            path = data_dir / f"{code}.csv"
            if not path.is_file():
                missing.append(str(path))
                continue
            try:
                frame = DataFetcher.load_stock_data(
                    code,
                    "2000-01-01",
                    "2100-01-01",
                    data_dir=str(data_dir),
                )
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                raise RuntimeError(
                    f"sector-guard index load failed for {code} (fail closed): {exc}"
                ) from exc
            if frame.empty or "close" not in frame.columns:
                missing.append(str(path))
                continue
            frames[code] = frame
        expected = set(REGIME_INDEX_FILES.values())
        if missing or set(frames) != expected:
            raise RuntimeError(
                "sector-guard index data incomplete (fail closed); missing: "
                + ", ".join(missing or sorted(expected - set(frames)))
            )
        self._sector_guard_index_frames = frames
        return frames

    def _sector_guard_observation_data(
        self, data_map: dict[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        """Return frames the sector guard should observe.

        Index modes ignore ``policy.regime_symbols`` for the guard path (those
        stocks remain the market-regime thermometer only). Off/regime_basket
        keeps the historical fixed signal-only basket.
        """
        mode = self._sector_guard_index_mode()
        if mode is None:
            return {
                code: data_map[code]
                for code in self.policy.regime_symbols
                if code in data_map
            }
        frames = self._ensure_sector_guard_index_frames()
        if mode == INDEX_MODE_TECH_ONLY:
            return {"000682": frames["000682"]}
        return {"000300": frames["000300"], "000682": frames["000682"]}

    def _index_day_metrics(
        self,
        frame: pd.DataFrame,
        date: pd.Timestamp,
        shock_ma: int,
        recovery_ma: int,
    ) -> dict[str, float | bool | pd.Series] | None:
        """Compute one index's shock/recovery inputs visible at ``date``."""
        max_ma = max(shock_ma, recovery_ma)
        history = frame.loc[frame.index <= date, "close"].dropna().astype(float)
        if len(history) <= max_ma or date not in history.index:
            return None
        current = float(history.iloc[-1])
        previous = float(history.iloc[-2])
        if current <= 0 or previous <= 0:
            return None
        daily_return = current / previous - 1.0
        above_shock = current > float(history.tail(shock_ma).mean())
        above_recovery = current > float(history.tail(recovery_ma).mean())
        normalized = history / float(history.iloc[0])
        return {
            "daily_return": daily_return,
            "above_shock_ma": above_shock,
            "above_recovery_ma": above_recovery,
            "normalized": normalized,
        }

    def _evaluate_index_shock_flags(
        self, date: pd.Timestamp
    ) -> dict[str, bool] | None:
        """Per-index shock flags for dual_confirm; None if any index incomplete."""
        frames = self._sector_guard_observation_data({})
        shock_ma = int(self.cfg["sector_shock_ma"])
        recovery_ma = int(self.cfg["sector_recovery_ma"])
        shock_return = float(self.cfg["sector_shock_return"])
        flags: dict[str, bool] = {}
        for code, frame in frames.items():
            metrics = self._index_day_metrics(frame, date, shock_ma, recovery_ma)
            if metrics is None:
                return None
            flags[code] = bool(
                float(metrics["daily_return"]) <= shock_return
                and not bool(metrics["above_shock_ma"])
            )
        return flags

    def _evaluate_index_recovery_flags(
        self,
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        pos: int,
        recovery_ma: int,
        shock_flags: dict[str, bool],
    ) -> dict[str, bool] | None:
        """Per-index recovery flags for dual_confirm; None if incomplete."""
        frames = self._sector_guard_observation_data({})
        shock_ma = int(self.cfg["sector_shock_ma"])
        flags: dict[str, bool] = {}
        recent_dates = all_dates[max(0, pos - recovery_ma + 1) : pos + 1]
        for code, frame in frames.items():
            metrics = self._index_day_metrics(frame, date, shock_ma, recovery_ma)
            if metrics is None:
                return None
            normalized = metrics["normalized"]
            assert isinstance(normalized, pd.Series)
            levels: list[float] = []
            for day in recent_dates:
                if day in normalized.index:
                    levels.append(float(normalized.loc[day]))
            index_above_ma = len(levels) >= recovery_ma and levels[-1] > float(
                np.mean(levels)
            )
            flags[code] = bool(
                not shock_flags.get(code, True)
                and float(metrics["daily_return"]) > 0.0
                and bool(metrics["above_recovery_ma"])
                and index_above_ma
            )
        return flags

    def _is_sector_shock(self, observation: SectorObservation) -> bool:
        """Index dual_confirm ANDs per-index shocks; otherwise inherit."""
        if self._sector_guard_index_mode() != INDEX_MODE_DUAL_CONFIRM:
            return super()._is_sector_shock(observation)  # pyright: ignore[reportAttributeAccessIssue]
        flags = getattr(self, "_last_index_shock_flags", None)
        if not flags or len(flags) < 2:
            return False
        return all(flags.values())

    def _is_sector_recovery(
        self,
        observation: SectorObservation,
        shock: bool,
        all_dates: list[pd.Timestamp],
        pos: int,
        recovery_ma: int,
    ) -> bool:
        """Index dual_confirm ANDs per-index recovery; otherwise inherit."""
        if self._sector_guard_index_mode() != INDEX_MODE_DUAL_CONFIRM:
            return super()._is_sector_recovery(  # pyright: ignore[reportAttributeAccessIssue]
                observation, shock, all_dates, pos, recovery_ma
            )
        if shock:
            return False
        flags = getattr(self, "_last_index_recovery_flags", None)
        if not flags or len(flags) < 2:
            return False
        return all(flags.values())

    def _update_sector_guard(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
    ) -> str | None:
        """Update breadth risk from indices (or regime basket), then regime SM.

        The regime update runs after the sector guard so entries respect the
        freshly scored regime, and before signal generation because
        ``_evaluate_trading_day`` continues only after this method returns.
        ``policy.regime_symbols`` remains the market-regime thermometer only.
        """
        mode = self._sector_guard_index_mode()
        if mode is not None:
            # Fail closed up front if index CSVs are absent rather than inventing.
            self._ensure_sector_guard_index_frames()
        scoped_data = self._sector_guard_observation_data(data_map)
        if mode == INDEX_MODE_DUAL_CONFIRM:
            shock_flags = self._evaluate_index_shock_flags(date)
            self._last_index_shock_flags = shock_flags or {}
            recovery_ma = int(self.cfg["sector_recovery_ma"])
            pos = date_to_pos[pd.Timestamp(date)]
            if shock_flags is not None:
                self._last_index_recovery_flags = (
                    self._evaluate_index_recovery_flags(
                        date, all_dates, pos, recovery_ma, shock_flags
                    )
                    or {}
                )
            else:
                self._last_index_recovery_flags = {}
        else:
            self._last_index_shock_flags = {}
            self._last_index_recovery_flags = {}
        guard_state = super()._update_sector_guard(  # pyright: ignore[reportAttributeAccessIssue]
            scoped_data,
            date,
            all_dates,
            date_to_pos,
        )
        self._update_market_regime(data_map, date, all_dates, date_to_pos)
        return guard_state

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Reset inherited one-shot logging whenever a temporary lock rearms."""
        before = len(self.risk_events)
        outcome = super()._apply_portfolio_risk(  # pyright: ignore[reportAttributeAccessIssue]
            current_assets, date_str, all_dates, date_to_pos, pending
        )
        if any(
            event.get("event") == "portfolio_drawdown_rearmed"
            for event in self.risk_events[before:]
        ):
            self._risk_lock_logged = False
        return outcome

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Expose temporary and terminal lock state plus regime history."""
        result = super()._build_result(  # pyright: ignore[reportAttributeAccessIssue]
            final_assets,
            all_dates,
        )
        manager = self.risk
        result.update(
            {
                "portfolio_policy": self.policy.as_dict(),
                "safe_mode_active": bool(getattr(self, "_safe_mode_active", False)),
                "terminal_risk_lock": bool(
                    isinstance(manager, RecoverableDrawdownRiskManager)
                    and manager.terminal_lock
                ),
                "cycle_lock_count": int(
                    manager.cycle_lock_count
                    if isinstance(manager, RecoverableDrawdownRiskManager)
                    else 0
                ),
                "guard_scope_mode": self._sector_guard_scope_mode(),
                "sector_guard_index_mode": self._sector_guard_index_mode(),
                "tradable_symbols": sorted(self._tradable_symbol_codes),
                "regime_state_series": list(self._regime_state_series),
                "regime_final_state": self._regime_state,
            }
        )
        return result
