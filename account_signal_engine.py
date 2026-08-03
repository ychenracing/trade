"""真实账户的时点决策支持，不向历史回测状态机注入实盘持仓。"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from numbers import Real
from pathlib import Path
from typing import Any, cast

import pandas as pd

import market_data_contracts
import quant_fusion as qf
import regime_adaptive as ra


_SYMBOL_RE = re.compile(r"^\d{6}$")
_EXPECTED_DATA_ERRORS = (
    ImportError,
    IndexError,
    KeyError,
    OSError,
    RuntimeError,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class AccountPosition:
    """描述账户快照中的一只实际持仓。"""

    symbol: str
    shares: int
    avg_cost: float
    entry_date: str
    highest_close: float | None = None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """保存现金、历史权益峰值和实际持仓的不可变快照。"""

    cash: float
    peak_equity: float
    positions: tuple[AccountPosition, ...]


def _require_real(name: str, value: object, *, minimum: float = 0.0) -> float:
    """拒绝布尔值和字符串，并返回满足下界的有限实数。"""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return normalized


def _require_positive_int(name: str, value: object) -> int:
    """只接受严格正整数，避免 JSON 布尔值或小数被静默转换。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_positive_real(name: str, value: object) -> float | None:
    """校验可选的正有限实数。"""
    if value is None:
        return None
    normalized = _require_real(name, value, minimum=0.0)
    if normalized <= 0:
        raise ValueError(f"{name} must be > 0 when provided")
    return normalized


def _validate_entry_date(value: object, *, symbol: str) -> str:
    """校验可选建仓日期并规范为 YYYY-MM-DD。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        timestamp = pd.Timestamp(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"position {symbol} has invalid entry_date") from exc
    if timestamp is pd.NaT:
        raise ValueError(f"position {symbol} has invalid entry_date")
    return cast(pd.Timestamp, timestamp).strftime("%Y-%m-%d")


def load_account_snapshot(path: str | Path) -> AccountSnapshot:
    """从严格 JSON 文件加载并验证真实账户快照。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read account snapshot: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("account snapshot root must be an object")

    cash = _require_real("account cash", payload.get("cash", 0.0))
    peak = _require_real("peak_equity", payload.get("peak_equity", cash))

    raw_positions = payload.get("positions", {})
    if not isinstance(raw_positions, dict):
        raise ValueError("positions must be an object keyed by stock code")

    positions: list[AccountPosition] = []
    for raw_code, raw in raw_positions.items():
        code = str(raw_code)
        if _SYMBOL_RE.fullmatch(code) is None:
            raise ValueError(f"invalid stock code in account snapshot: {code!r}")
        if not isinstance(raw, dict):
            raise ValueError(f"position {code} must be an object")

        shares = _require_positive_int(f"position {code} shares", raw.get("shares", 0))
        avg_cost = _require_real(
            f"position {code} avg_cost",
            raw.get("avg_cost", raw.get("price", 0.0)),
        )
        if avg_cost <= 0:
            raise ValueError(f"position {code} avg_cost must be > 0")
        highest_close = _optional_positive_real(
            f"position {code} highest_close",
            raw.get("highest_close"),
        )
        positions.append(
            AccountPosition(
                symbol=code,
                shares=shares,
                avg_cost=avg_cost,
                entry_date=_validate_entry_date(raw.get("entry_date", ""), symbol=code),
                highest_close=highest_close,
            )
        )

    positions.sort(key=lambda item: item.symbol)
    return AccountSnapshot(cash=cash, peak_equity=peak, positions=tuple(positions))


class AccountSignalEngine:
    """基于真实持仓生成时点建议，不伪造实盘历史收益曲线。"""

    def __init__(self, *, cache_dir: str, regime_data_dir: str) -> None:
        """绑定股票缓存目录和固定指数证据目录。"""
        self.cache_dir = cache_dir
        self.regime_data_dir = regime_data_dir

    def _frame(self, code: str, as_of: str) -> pd.DataFrame:
        """加载截至指定日期的新鲜行情，并拒绝未来或陈旧观测。"""
        boundary = pd.Timestamp(as_of)
        if boundary is pd.NaT:
            raise ValueError("as_of must resolve to a valid date")
        boundary = cast(pd.Timestamp, boundary).normalize()
        start = (boundary - pd.Timedelta(days=700)).strftime("%Y-%m-%d")
        frame = qf.DataFetcher.load_stock_data(
            code,
            start,
            boundary.strftime("%Y-%m-%d"),
            data_dir=None,
        )
        if frame.empty:
            raise ValueError("no market data")
        observed = pd.Timestamp(frame.index[-1])
        if observed is pd.NaT:
            raise ValueError("latest market-data date is invalid")
        observed = cast(pd.Timestamp, observed).normalize()
        if observed > boundary:
            raise ValueError("market data contains a future observation")
        if (boundary - observed).days > ra.MAX_EVIDENCE_STALENESS_DAYS:
            raise ValueError(
                f"latest market data is stale: {observed.date()} "
                f"(as of {boundary.date()})"
            )
        return frame

    @staticmethod
    def _latest_value(series: pd.Series | None, index: int) -> float:
        """读取指标末值；缺失或非有限值返回 NaN 供调用者显式判断。"""
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
        """生成持仓处置建议、候选买入和可审计估值完整性字段。"""
        qf.DataFetcher._cache_dir = self.cache_dir
        market_data_contracts.refresh_regime_indices(
            self.regime_data_dir,
            end_date=as_of,
            strict=False,
        )
        decision = ra.RegimeAdaptiveBacktestEngine().decide_current(
            symbols,
            as_of=as_of,
            data_dir=self.regime_data_dir,
            leader_data_dir=self.cache_dir,
        )

        held = {position.symbol: position for position in snapshot.positions}
        actions: list[dict[str, Any]] = []
        priced_market_value = 0.0
        unpriced_symbols: list[str] = []
        as_of_timestamp = pd.Timestamp(as_of)
        if as_of_timestamp is pd.NaT:
            raise ValueError("as_of must resolve to a valid date")
        as_of_timestamp = cast(pd.Timestamp, as_of_timestamp).normalize()

        for position in snapshot.positions:
            code = position.symbol
            name = symbols.get(code, code)
            try:
                frame = self._frame(code, as_of)
                cfg = qf.BacktestEngine.config_for_symbol(code, name=name)
                indicators = qf.Indicators.compute_all(frame, cfg)
                i = len(frame) - 1
                close = float(frame["close"].iloc[i])
                if not math.isfinite(close) or close <= 0:
                    raise ValueError("latest close must be finite and positive")
                priced_market_value += position.shares * close

                atr = self._latest_value(indicators.get("atr"), i)
                ma_short = self._latest_value(indicators.get("ma_short"), i)
                ma_long = self._latest_value(indicators.get("ma_long"), i)
                if position.entry_date:
                    entry_timestamp = pd.Timestamp(position.entry_date).normalize()
                    if entry_timestamp > as_of_timestamp:
                        raise ValueError(
                            f"position {code} entry_date is later than as_of"
                        )
                    since_entry = frame.loc[
                        frame.index >= entry_timestamp,
                        "close",
                    ]
                else:
                    since_entry = frame["close"]
                observed_peak = (
                    float(cast(Any, since_entry.max()))
                    if not since_entry.empty
                    else close
                )
                if not math.isfinite(observed_peak) or observed_peak <= 0:
                    observed_peak = close
                peak = max(observed_peak, position.highest_close or 0.0, close)

                hard_stop = position.avg_cost * (
                    1.0 - float(cfg.get("hard_stop", 0.15))
                )
                active_stop = hard_stop
                peak_gain = peak / position.avg_cost - 1.0
                if (
                    math.isfinite(atr)
                    and atr > 0
                    and peak_gain
                    >= float(cfg.get("profit_lock_activation", 0.2))
                ):
                    active_stop = max(
                        active_stop,
                        peak - float(cfg.get("trail_atr_mult", 4.0)) * atr,
                    )

                reasons: list[str] = []
                action = "HOLD"
                if close <= active_stop:
                    action = "SELL"
                    reasons.append(
                        f"close {close:.2f} <= protective stop {active_stop:.2f}"
                    )
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
                    reasons.append(
                        "holding is outside the current weak-regime leaders"
                    )
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
            except _EXPECTED_DATA_ERRORS as exc:
                unpriced_symbols.append(code)
                actions.append(
                    {
                        "symbol": code,
                        "name": name,
                        "action": "DATA_ERROR",
                        "shares": position.shares,
                        "reason": str(exc),
                    }
                )

        valuation_complete = not unpriced_symbols
        selected = (
            decision.leaders.selected_symbols
            if decision.leaders is not None
            else ()
        )
        if (
            valuation_complete
            and decision.name == "positive_momentum_hold"
            and snapshot.cash > 0
        ):
            for code in selected:
                if code not in held:
                    actions.append(
                        {
                            "symbol": code,
                            "name": symbols.get(code, code),
                            "action": "BUY_CANDIDATE",
                            "shares": 0,
                            "reason": (
                                "current positive-240-session weak-regime leader"
                            ),
                        }
                    )

        estimated_market_value: float | None = (
            priced_market_value if valuation_complete else None
        )
        estimated_equity: float | None = (
            snapshot.cash + priced_market_value if valuation_complete else None
        )
        return {
            "as_of": as_of,
            "mode": "account_decision_support",
            "cash": snapshot.cash,
            "priced_market_value": priced_market_value,
            "estimated_market_value": estimated_market_value,
            "estimated_equity": estimated_equity,
            "valuation_complete": valuation_complete,
            "unpriced_symbols": sorted(unpriced_symbols),
            "buys_suppressed": not valuation_complete,
            "peak_equity": snapshot.peak_equity,
            "deployment_decision": asdict(decision),
            "actions": actions,
            "disclaimer": (
                "Decision support only. Orders are not sent to a broker and "
                "share quantities require manual confirmation."
            ),
        }


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    """以临时文件、刷盘和原子替换方式写入严格 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".account_",
        suffix=".tmp",
    )
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
    """运行账户扫描，写入工件并返回适合脚本调用的退出码。"""
    try:
        snapshot = load_account_snapshot(account_path)
        result = AccountSignalEngine(
            cache_dir=cache_dir,
            regime_data_dir=regime_data_dir,
        ).run(snapshot, symbols, as_of=end_date)
        output = Path(output_dir) / f"account_signals_{end_date}.json"
        _atomic_json(result, output)
    except _EXPECTED_DATA_ERRORS as exc:
        print(f"Account signal scan failed: {exc}")
        return 1

    print("=" * 72)
    print("  Real-account decision support")
    print("=" * 72)
    print(f"  As of: {end_date}")
    if result["estimated_equity"] is None:
        print(
            "  Estimated equity: unavailable "
            f"(unpriced holdings: {', '.join(result['unpriced_symbols'])})"
        )
    else:
        print(f"  Estimated equity: {result['estimated_equity']:,.0f}")
    print(f"  Route: {result['deployment_decision']['name']}")
    for action in result["actions"]:
        print(
            f"  {action['symbol']} {action['name']}: {action['action']} | "
            f"{action['reason']}"
        )
    print(f"  Artifact: {output}")
    return 0
