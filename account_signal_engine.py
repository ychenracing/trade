"""Real-account decision support kept separate from the backtest state machine."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import pandas as pd

import market_data_contracts
import quant_fusion as qf
import regime_adaptive as ra


@dataclass(frozen=True, slots=True)
class AccountPosition:
    symbol: str
    shares: int
    avg_cost: float
    entry_date: str
    highest_close: float | None = None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    cash: float
    peak_equity: float
    positions: tuple[AccountPosition, ...]


def load_account_snapshot(path: str | Path) -> AccountSnapshot:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cash = float(payload.get("cash", 0.0))
    peak = float(payload.get("peak_equity", cash))
    if not math.isfinite(cash) or cash < 0:
        raise ValueError("account cash must be finite and non-negative")
    if not math.isfinite(peak) or peak < 0:
        raise ValueError("peak_equity must be finite and non-negative")
    positions: list[AccountPosition] = []
    raw_positions = payload.get("positions", {})
    if not isinstance(raw_positions, dict):
        raise ValueError("positions must be an object keyed by stock code")
    for code, raw in raw_positions.items():
        if not isinstance(raw, dict):
            raise ValueError(f"position {code} must be an object")
        shares = int(raw.get("shares", 0))
        avg_cost = float(raw.get("avg_cost", raw.get("price", 0.0)))
        if shares <= 0 or avg_cost <= 0 or not math.isfinite(avg_cost):
            raise ValueError(f"position {code} has invalid shares or average cost")
        positions.append(
            AccountPosition(
                symbol=str(code),
                shares=shares,
                avg_cost=avg_cost,
                entry_date=str(raw.get("entry_date", "")),
                highest_close=(
                    float(raw["highest_close"])
                    if raw.get("highest_close") is not None
                    else None
                ),
            )
        )
    return AccountSnapshot(cash=cash, peak_equity=peak, positions=tuple(positions))


class AccountSignalEngine:
    """Generate point-in-time advice from actual holdings without fake PnL history."""

    def __init__(self, *, cache_dir: str, regime_data_dir: str) -> None:
        self.cache_dir = cache_dir
        self.regime_data_dir = regime_data_dir

    def _frame(self, code: str, as_of: str) -> pd.DataFrame:
        start = (pd.Timestamp(as_of) - pd.Timedelta(days=700)).strftime("%Y-%m-%d")
        return qf.DataFetcher.load_stock_data(code, start, as_of, data_dir=None)

    @staticmethod
    def _latest_value(series: pd.Series | None, index: int) -> float:
        if series is None:
            return float("nan")
        value = float(series.iloc[index])
        return value if math.isfinite(value) else float("nan")

    def run(
        self,
        snapshot: AccountSnapshot,
        symbols: dict[str, str],
        *,
        as_of: str,
    ) -> dict[str, Any]:
        qf.DataFetcher._cache_dir = self.cache_dir
        market_data_contracts.refresh_regime_indices(
            self.regime_data_dir, end_date=as_of, strict=False
        )
        decision = ra.RegimeAdaptiveBacktestEngine().decide_current(
            symbols,
            as_of=as_of,
            data_dir=self.regime_data_dir,
            leader_data_dir=self.cache_dir,
        )
        held = {position.symbol: position for position in snapshot.positions}
        actions: list[dict[str, Any]] = []
        market_value = 0.0
        for position in snapshot.positions:
            code = position.symbol
            name = symbols.get(code, code)
            try:
                frame = self._frame(code, as_of)
                if frame.empty:
                    raise ValueError("no data")
                cfg = qf.BacktestEngine.config_for_symbol(code, name=name)
                indicators = qf.Indicators.compute_all(frame, cfg)
                i = len(frame) - 1
                close = float(frame["close"].iloc[i])
                market_value += position.shares * close
                atr = self._latest_value(indicators.get("atr"), i)
                ma_short = self._latest_value(indicators.get("ma_short"), i)
                ma_long = self._latest_value(indicators.get("ma_long"), i)
                if position.entry_date:
                    since_entry = frame.loc[frame.index >= pd.Timestamp(position.entry_date), "close"]
                else:
                    since_entry = frame["close"]
                observed_peak = (
                    float(cast(Any, since_entry.max()))
                    if not since_entry.empty
                    else close
                )
                peak = max(observed_peak, position.highest_close or 0.0, close)
                hard_stop = position.avg_cost * (1.0 - float(cfg.get("hard_stop", 0.15)))
                active_stop = hard_stop
                peak_gain = peak / position.avg_cost - 1.0
                if math.isfinite(atr) and atr > 0 and peak_gain >= float(
                    cfg.get("profit_lock_activation", 0.2)
                ):
                    active_stop = max(
                        active_stop,
                        peak - float(cfg.get("trail_atr_mult", 4.0)) * atr,
                    )
                reasons: list[str] = []
                action = "HOLD"
                if close <= active_stop:
                    action = "SELL"
                    reasons.append(f"close {close:.2f} <= protective stop {active_stop:.2f}")
                elif (
                    math.isfinite(ma_short)
                    and math.isfinite(ma_long)
                    and ma_short < ma_long
                    and close < ma_short
                ):
                    action = "SELL"
                    reasons.append("short trend is below the long trend")
                elif decision.name == "cash_preservation":
                    action = "REDUCE_REVIEW"
                    reasons.append("current route is cash preservation")
                elif (
                    decision.name == "positive_momentum_hold"
                    and decision.leaders is not None
                    and code not in decision.leaders.selected_symbols
                ):
                    action = "REDUCE_REVIEW"
                    reasons.append("holding is outside the current weak-regime leaders")
                else:
                    reasons.append("no account-specific exit condition")
                actions.append(
                    {
                        "symbol": code,
                        "name": name,
                        "action": action,
                        "shares": position.shares,
                        "avg_cost": position.avg_cost,
                        "close": close,
                        "protective_stop": active_stop,
                        "peak_close": peak,
                        "reason": "; ".join(reasons),
                    }
                )
            except Exception as exc:
                actions.append(
                    {
                        "symbol": code,
                        "name": name,
                        "action": "DATA_ERROR",
                        "shares": position.shares,
                        "reason": str(exc),
                    }
                )
        selected = (
            decision.leaders.selected_symbols
            if decision.leaders is not None
            else ()
        )
        if decision.name == "positive_momentum_hold" and snapshot.cash > 0:
            for code in selected:
                if code not in held:
                    actions.append(
                        {
                            "symbol": code,
                            "name": symbols.get(code, code),
                            "action": "BUY_CANDIDATE",
                            "shares": 0,
                            "reason": "current positive-240-session weak-regime leader",
                        }
                    )
        return {
            "as_of": as_of,
            "mode": "account_decision_support",
            "cash": snapshot.cash,
            "estimated_market_value": market_value,
            "estimated_equity": snapshot.cash + market_value,
            "peak_equity": snapshot.peak_equity,
            "deployment_decision": asdict(decision),
            "actions": actions,
            "disclaimer": (
                "Decision support only. Orders are not sent to a broker and "
                "share quantities require manual confirmation."
            ),
        }


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".account_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_account_scan(
    *,
    account_path: str,
    symbols: dict[str, str],
    end_date: str,
    cache_dir: str,
    regime_data_dir: str,
    output_dir: str,
) -> int:
    try:
        snapshot = load_account_snapshot(account_path)
        result = AccountSignalEngine(
            cache_dir=cache_dir,
            regime_data_dir=regime_data_dir,
        ).run(snapshot, symbols, as_of=end_date)
        output = Path(output_dir) / f"account_signals_{end_date}.json"
        _atomic_json(result, output)
    except Exception as exc:
        print(f"Account signal scan failed: {exc}")
        return 1
    print("=" * 72)
    print("  Real-account decision support")
    print("=" * 72)
    print(f"  As of: {end_date}")
    print(f"  Estimated equity: {result['estimated_equity']:,.0f}")
    print(f"  Route: {result['deployment_decision']['name']}")
    for action in result["actions"]:
        print(
            f"  {action['symbol']} {action['name']}: {action['action']} | "
            f"{action['reason']}"
        )
    print(f"  Artifact: {output}")
    return 0
