"""Lag-safe technical indicators used by all strategies."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quantfusion.domain.rules import require_int

_require_int = require_int

class Indicators:
    """Compute lag-safe technical indicators used by all strategies."""

    @staticmethod
    def _wilder_average(series: pd.Series, period: int) -> pd.Series:
        """Compute Wilder smoothing after one complete non-missing seed."""
        period = _require_int("period", period, min_value=1)
        values = pd.to_numeric(series, errors="coerce")
        out = pd.Series(np.nan, index=values.index, dtype="float64")
        if len(values) < period:
            return out
        valid_counts = values.notna().rolling(period, min_periods=period).sum()
        seed_positions = np.flatnonzero(valid_counts.to_numpy() >= period)
        if len(seed_positions) == 0:
            return out
        seed_pos = int(seed_positions[0])
        seed_window = values.iloc[seed_pos - period + 1 : seed_pos + 1]
        first = seed_window.mean()
        out.iloc[seed_pos] = first
        for idx in range(seed_pos + 1, len(values)):
            current = values.iloc[idx]
            prev = out.iloc[idx - 1]
            if pd.isna(prev):
                # The smoothed value was lost (only possible if the whole series
                # between here and the seed was missing); re-seed from the most
                # recent complete window instead of letting the chain die.
                window = values.iloc[max(0, idx - period + 1) : idx + 1].dropna()
                if len(window) < period:
                    continue
                out.iloc[idx] = window.iloc[-period:].mean()
                continue
            if pd.isna(current):
                # A transient gap in the input (e.g. ADX's dx when both +DI and
                # -DI are 0 in a flat/symmetric tape): hold the previous smoothed
                # value so a single missing point does not permanently kill the
                # Wilder smoothing from that bar onward.
                out.iloc[idx] = prev
                continue
            out.iloc[idx] = (prev * (period - 1) + current) / period
        return out

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 20, method: str = "wilder") -> pd.Series:
        """Compute average true range with Wilder or simple smoothing."""
        period = _require_int("period", period, min_value=1)
        method = str(method).lower()
        if method not in {"wilder", "sma"}:
            raise ValueError(
                f"method must be 'wilder' or 'sma'; current value is {method!r}"
            )
        high, low, close = (df["high"], df["low"], df["close"])
        prev_close = close.shift(1)
        tr = pd.concat(
            [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        if method == "sma":
            return tr.rolling(period, min_periods=period).mean()
        return Indicators._wilder_average(tr, period)

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Compute average directional index from OHLC data."""
        period = _require_int("period", period, min_value=1)
        high, low = (df["high"], df["low"])
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=df.index,
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=df.index,
        )
        atr_val = Indicators.atr(df, period)
        atr_safe = atr_val.replace(0, np.nan)
        plus_smoothed = Indicators._wilder_average(plus_dm, period)
        minus_smoothed = Indicators._wilder_average(minus_dm, period)
        plus_di = 100 * plus_smoothed / atr_safe
        minus_di = 100 * minus_smoothed / atr_safe
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_val = Indicators._wilder_average(dx.dropna().reindex(df.index), period)
        return adx_val.fillna(0)

    @staticmethod
    def rsi(close: pd.Series, period: int = 14) -> pd.Series:
        """Compute Wilder RSI, mapping unchanged windows to neutral."""
        period = _require_int("period", period, min_value=1)
        delta = close.diff()
        gain = delta.clip(lower=0).fillna(0.0)
        loss = (-delta).clip(lower=0).fillna(0.0)
        avg_gain = Indicators._wilder_average(gain, period)
        avg_loss = Indicators._wilder_average(loss, period)
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_val = 100 - 100 / (1 + rs)
        fill_values = pd.Series(np.where(avg_gain > 0, 100.0, 50.0), index=close.index)
        return rsi_val.fillna(fill_values).fillna(50.0)

    @staticmethod
    def donchian(
        df: pd.DataFrame, entry_period: int = 20, exit_period: int = 10
    ) -> tuple[pd.Series, pd.Series]:
        """Return lagged Donchian bands without look-ahead."""
        entry_period = _require_int("entry_period", entry_period, min_value=1)
        exit_period = _require_int("exit_period", exit_period, min_value=1)
        # The one-bar shift is essential: today's high and low must not influence
        # a breakout decision made at today's close.
        upper = df["high"].rolling(entry_period).max().shift(1)
        lower = df["low"].rolling(exit_period).min().shift(1)
        return (upper, lower)

    @staticmethod
    def ma(series: pd.Series, period: int) -> pd.Series:
        """Return a full-window simple moving average."""
        period = _require_int("period", period, min_value=1)
        return series.rolling(period).mean()

    @staticmethod
    def hurst_rs(series: pd.Series, window: int = 100) -> float:
        """Estimate the Hurst exponent via rescaled-range (R/S) analysis.

        The function is a pure transformation: it consumes the most recent
        ``window`` observations of the supplied series (callers should pass a
        stationary transformation such as log returns for price data) and
        regresses ``log(R/S)`` on ``log(n)`` across several sub-period sizes.
        Values near 0.5 indicate a random walk, above 0.5 persistence
        (trending), and below 0.5 anti-persistence (mean-reversion). A finite
        result requires at least ``window`` non-missing observations.
        """
        period = _require_int("window", window, min_value=10)
        values = (
            pd.Series(pd.to_numeric(series, errors="coerce"))
            .dropna()
            .to_numpy(dtype=float)
        )
        if values.size < period:
            return float("nan")
        segment = values[-period:]
        n = segment.size
        # Build a stable set of sub-period sizes that divide the window so the
        # log-log regression has at least two support points.
        sizes: list[int] = []
        size = 4
        while size <= n // 2:
            sizes.append(size)
            size *= 2
        for divisor in (2, 3, 5):
            candidate = n // divisor
            if candidate >= 4 and candidate not in sizes:
                sizes.append(candidate)
        sizes = sorted(set(sizes))
        if len(sizes) < 2:
            return float("nan")
        log_sizes: list[float] = []
        log_rs: list[float] = []
        for size in sizes:
            blocks = n // size
            if blocks < 1:
                continue
            rs_values: list[float] = []
            for block in range(blocks):
                sub = segment[block * size : (block + 1) * size]
                mean = float(np.mean(sub))
                deviation = np.cumsum(sub - mean)
                r = float(np.max(deviation) - np.min(deviation))
                std = float(np.std(sub, ddof=0))
                if std > 0.0 and r > 0.0:
                    rs_values.append(r / std)
            if rs_values:
                log_sizes.append(math.log(size))
                log_rs.append(math.log(float(np.mean(rs_values))))
        if len(log_sizes) < 2:
            return float("nan")
        slope = float(np.polyfit(np.array(log_sizes), np.array(log_rs), 1)[0])
        if not math.isfinite(slope):
            return float("nan")
        return max(0.0, min(1.0, slope))

    @staticmethod
    def compute_all(df: pd.DataFrame, cfg: dict) -> dict[str, pd.Series]:
        """Precompute every indicator consumed by the strategies."""
        atr_period = cfg.get("atr_period", 20)
        adx_period = cfg.get("adx_period", 14)
        rsi_period = cfg.get("rsi_period", 14)
        entry_p = cfg.get("entry_period", 20)
        exit_p = cfg.get("exit_period", 10)
        ma_short = cfg.get("ma_short", 20)
        ma_long = cfg.get("ma_long", 60)
        reversal_exit_period = int(cfg.get("reversal_exit_period", 6))
        donchian_upper, donchian_lower = Indicators.donchian(df, entry_p, exit_p)
        return {
            "atr": Indicators.atr(
                df, atr_period, method=str(cfg.get("atr_method", "wilder"))
            ),
            "adx": Indicators.adx(df, adx_period),
            "rsi": Indicators.rsi(df["close"], rsi_period),
            "donchian_upper": donchian_upper,
            "donchian_lower": donchian_lower,
            "ma_short": Indicators.ma(df["close"], ma_short),
            "ma_long": Indicators.ma(df["close"], ma_long),
            # The reversal floor is lagged: today's low cannot affect today's
            # close-generated exit signal.
            "reversal_low": (df["low"].rolling(reversal_exit_period).min().shift(1)),
        }
