"""Account identity, market-evidence, and advisory-output boundaries."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from quantfusion.account.models import AccountPosition, AccountSnapshot
from quantfusion.application import account_scan
from quantfusion.engine import replay as replay_module
from quantfusion.regime.models import (
    DeploymentDecision,
    LeaderSelection,
    RegimeEvidence,
    RegimeRoute,
)

AS_OF = "2026-02-01"


def _snapshot(*, account_id: str = "main", snapshot_date: str = AS_OF) -> AccountSnapshot:
    return AccountSnapshot(
        schema_version=3,
        account_id=account_id,
        snapshot_date=snapshot_date,
        cash=100_000.0,
        peak_equity=100_000.0,
        positions=(),
    )


def _cash_decision() -> DeploymentDecision:
    return DeploymentDecision(
        name="cash_preservation",
        boundary=AS_OF,
        reason="test",
        regime=RegimeEvidence(as_of=AS_OF, regime="unknown", observations=()),
        leaders=None,
    )


def _decision(
    name: str,
    *,
    selected: tuple[str, ...] = (),
) -> DeploymentDecision:
    leaders = (
        LeaderSelection(
            as_of=AS_OF,
            requested_symbols=selected,
            observed_symbols=len(selected),
            selected_symbols=selected,
            selected_returns=tuple(0.1 for _ in selected),
        )
        if name == "positive_momentum_hold"
        else None
    )
    return DeploymentDecision(
        name=name,
        boundary=AS_OF,
        reason="test",
        regime=RegimeEvidence(as_of=AS_OF, regime="trending", observations=()),
        leaders=leaders,
    )


def _frame(
    *,
    end: str = "2026-01-30",
    close: float = 100.0,
    peak: float | None = None,
) -> pd.DataFrame:
    index = pd.bdate_range(end=end, periods=100)
    closes = [close] * len(index)
    if peak is not None:
        closes[-20] = peak
    return pd.DataFrame(
        {
            "open": closes,
            "close": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "volume": 1_000_000.0,
        },
        index=index,
    )


def _indicators(
    frame: pd.DataFrame,
    config: dict[str, object] | None = None,
) -> dict[str, pd.Series]:
    return {
        "atr": pd.Series(1.0, index=frame.index),
        "ma_short": pd.Series(101.0, index=frame.index),
        "ma_long": pd.Series(99.0, index=frame.index),
    }


class _BuyStrategy:
    name = "turtle_breakout"

    def __init__(self, config: dict[str, object]) -> None:
        self.config = config

    def on_bar(self, context: object) -> SimpleNamespace:
        return SimpleNamespace(direction="buy", stop_loss=90.0)


class _NoBuyStrategy:
    name = "dual_ma"

    def __init__(self, config: dict[str, object]) -> None:
        self.config = config

    def on_bar(self, context: object) -> None:
        return None


def _holding_snapshot(
    *,
    cash: float = 100_000.0,
    entry_date: str = "2025-10-01",
    highest_close: float | None = None,
) -> AccountSnapshot:
    return AccountSnapshot(
        schema_version=3,
        account_id="main",
        snapshot_date=AS_OF,
        cash=cash,
        peak_equity=200_000.0,
        positions=(
            AccountPosition(
                symbol="300308",
                shares=100,
                sellable_shares=40,
                avg_cost=100.0,
                entry_date=entry_date,
                highest_close=highest_close,
            ),
        ),
    )


def _run_with_market(
    *,
    snapshot: AccountSnapshot,
    symbols: dict[str, str],
    decision: DeploymentDecision,
    frames: dict[str, pd.DataFrame | Exception],
) -> tuple[dict[str, object], Counter[str]]:
    engine = account_scan.AccountSignalEngine(
        cache_dir="unused",
        regime_data_dir="unused",
    )
    calls: Counter[str] = Counter()

    def _load(code: str, as_of: str) -> pd.DataFrame:
        calls[code] += 1
        value = frames[code]
        if isinstance(value, Exception):
            raise value
        return value

    config = {
        "hard_stop": 0.15,
        "profit_lock_activation": 0.20,
        "trail_atr_mult": 4.0,
    }
    with (
        patch.object(
            account_scan.data_contracts,
            "refresh_regime_indices",
            return_value={},
        ),
        patch.object(
            account_scan.RegimeAdaptiveBacktestEngine,
            "decide_current",
            return_value=decision,
        ),
        patch.object(engine, "_frame", side_effect=_load),
        patch.object(
            account_scan.BacktestEngine,
            "config_for_symbol",
            return_value=config,
        ),
        patch.object(
            account_scan.Indicators,
            "compute_all",
            side_effect=_indicators,
        ),
        patch.object(account_scan, "TurtleBreakoutStrategy", _BuyStrategy),
        patch.object(account_scan, "DualMAStrategy", _NoBuyStrategy),
        patch.object(account_scan, "ATRChannelStrategy", _NoBuyStrategy),
        patch.object(account_scan, "trend_candidate_score", return_value=0.9),
        patch.object(account_scan, "default_engine_config", return_value={"max_positions": 6}),
    ):
        result = engine.run(
            snapshot,
            symbols,
            as_of=AS_OF,
            expected_account_id="main",
        )
    return result, calls


@pytest.mark.parametrize(
    ("snapshot", "expected_account_id", "expected_error"),
    [
        (
            _snapshot(account_id="other"),
            "main",
            "expected account_id='main'.*actual='other'",
        ),
        (
            _snapshot(snapshot_date="2026-01-31"),
            "main",
            "snapshot_date='2026-01-31'.*requested_as_of='2026-02-01'",
        ),
    ],
)
def test_identity_mismatches_fail_before_any_market_request(
    snapshot: AccountSnapshot,
    expected_account_id: str,
    expected_error: str,
) -> None:
    engine = account_scan.AccountSignalEngine(
        cache_dir="unused",
        regime_data_dir="unused",
    )
    with (
        patch.object(account_scan.data_contracts, "refresh_regime_indices") as refresh,
        patch.object(
            account_scan.RegimeAdaptiveBacktestEngine,
            "decide_current",
        ) as decide,
        patch.object(engine, "_frame") as load_frame,
    ):
        with pytest.raises(ValueError, match=expected_error):
            engine.run(
                snapshot,
                {},
                as_of=AS_OF,
                expected_account_id=expected_account_id,
            )

    refresh.assert_not_called()
    decide.assert_not_called()
    load_frame.assert_not_called()


def test_matching_identity_and_date_are_written_to_result() -> None:
    engine = account_scan.AccountSignalEngine(
        cache_dir="unused",
        regime_data_dir="unused",
    )
    with (
        patch.object(
            account_scan.data_contracts,
            "refresh_regime_indices",
            return_value={},
        ),
        patch.object(
            account_scan.RegimeAdaptiveBacktestEngine,
            "decide_current",
            return_value=_cash_decision(),
        ),
    ):
        result = engine.run(
            _snapshot(),
            {},
            as_of=AS_OF,
            expected_account_id="main",
        )

    assert result["account_id"] == "main"
    assert result["snapshot_date"] == AS_OF
    assert result["requested_as_of"] == AS_OF


def test_cli_artifact_records_exact_snapshot_sha256(tmp_path: Path) -> None:
    raw = (
        b'{"schema_version":3,"account_id":"main",'
        b'"snapshot_date":"2026-02-01","cash":100000,'
        b'"peak_equity":100000,"positions":{}}\n'
    )
    account_path = tmp_path / "account.json"
    account_path.write_bytes(raw)
    captured: dict[str, object] = {}

    def _capture(payload: dict[str, object], path: Path) -> None:
        captured.update(payload)

    with (
        patch.object(
            account_scan.AccountSignalEngine,
            "run",
            return_value={
                "estimated_equity": 100_000.0,
                "deployment_decision": {"name": "cash_preservation"},
                "actions": [],
            },
        ) as run,
        patch.object(account_scan, "atomic_json", side_effect=_capture) as atomic,
    ):
        exit_code = account_scan.run_account_scan(
            account_path=str(account_path),
            expected_account_id="main",
            symbols={},
            end_date=AS_OF,
            cache_dir="unused",
            regime_data_dir="unused",
            output_dir=str(tmp_path),
        )

    assert exit_code == 0
    assert captured["account_snapshot_sha256"] == hashlib.sha256(raw).hexdigest()
    run.assert_called_once()
    assert run.call_args.kwargs["expected_account_id"] == "main"
    atomic.assert_called_once()
    json.dumps(captured, allow_nan=False)


def test_account_frame_rejects_provider_marked_stale_with_cache_date() -> None:
    engine = account_scan.AccountSignalEngine(
        cache_dir="unused",
        regime_data_dir="unused",
    )
    frame = _frame(end="2026-01-30")
    frame.attrs["_stale"] = True
    frame.attrs["_cache_last_date"] = "2026-01-30"

    with patch.object(
        account_scan.DataFetcher,
        "load_stock_data",
        return_value=frame,
    ):
        with pytest.raises(
            ValueError,
            match="300308.*cache_last_date=2026-01-30",
        ):
            engine._frame("300308", AS_OF)


def test_frozen_trend_loads_each_symbol_once_per_scan() -> None:
    result, calls = _run_with_market(
        snapshot=_holding_snapshot(),
        symbols={"300308": "持仓", "300502": "候选"},
        decision=_decision("frozen_trend_engine"),
        frames={"300308": _frame(), "300502": _frame()},
    )

    assert calls == Counter({"300308": 1, "300502": 1})
    assert result["data_complete"] is True
    assert result["evidence_date"] == "2026-01-30"


def test_missing_candidate_suppresses_all_buys_but_preserves_sell() -> None:
    result, _ = _run_with_market(
        snapshot=_holding_snapshot(),
        symbols={
            "300308": "持仓",
            "300502": "缺失候选",
            "300394": "可用候选",
        },
        decision=_decision("frozen_trend_engine"),
        frames={
            "300308": _frame(close=80.0),
            "300502": ValueError("provider unavailable"),
            "300394": _frame(),
        },
    )

    held = next(item for item in result["actions"] if item["symbol"] == "300308")
    assert held["action"] == "SELL"
    assert held["recommended_shares"] == 40
    assert not any(item["action"] == "BUY_CANDIDATE" for item in result["actions"])
    assert result["data_complete"] is False
    assert result["unavailable_symbols"] == ["300502"]
    assert result["buys_suppressed"] is True
    assert result["buy_suppression_reasons"] == [
        "MARKET_DATA_UNAVAILABLE:300502"
    ]
    json.dumps(result, allow_nan=False, sort_keys=True)


def test_mixed_actual_evidence_dates_suppress_buys_but_preserve_sell() -> None:
    result, _ = _run_with_market(
        snapshot=_holding_snapshot(),
        symbols={"300308": "持仓", "300502": "候选"},
        decision=_decision("frozen_trend_engine"),
        frames={
            "300308": _frame(end="2026-01-30", close=80.0),
            "300502": _frame(end="2026-01-29"),
        },
    )

    held = next(item for item in result["actions"] if item["symbol"] == "300308")
    assert held["action"] == "SELL"
    assert not any(item["action"] == "BUY_CANDIDATE" for item in result["actions"])
    assert result["data_complete"] is False
    assert result["evidence_date"] is None
    assert result["unavailable_symbols"] == ["300308", "300502"]
    assert result["buy_suppression_reasons"] == [
        "INCONSISTENT_EVIDENCE_DATE:300308=2026-01-30",
        "INCONSISTENT_EVIDENCE_DATE:300502=2026-01-29",
    ]


def test_trend_buy_candidate_is_indicative_and_names_strategies() -> None:
    result, _ = _run_with_market(
        snapshot=_snapshot(),
        symbols={"300502": "候选"},
        decision=_decision("frozen_trend_engine"),
        frames={"300502": _frame()},
    )

    candidate = next(
        item for item in result["actions"] if item["action"] == "BUY_CANDIDATE"
    )
    assert candidate["shares"] == 0
    assert candidate["indicative_target_shares"] >= 0
    assert candidate["indicative_target_shares"] % 100 == 0
    assert candidate["execution_status"] == "INDICATIVE_REVIEW_ONLY"
    assert candidate["strategies"] == ["turtle_breakout"]
    assert "sleeves" not in candidate
    assert "收盘价估算" in candidate["reason"]
    assert "下一可交易日开盘价格" in candidate["reason"]


def test_weak_buy_candidate_uses_the_same_non_execution_contract() -> None:
    result, calls = _run_with_market(
        snapshot=_snapshot(),
        symbols={"300502": "候选"},
        decision=_decision("positive_momentum_hold", selected=("300502",)),
        frames={"300502": _frame()},
    )

    candidate = next(
        item for item in result["actions"] if item["action"] == "BUY_CANDIDATE"
    )
    assert calls == Counter({"300502": 1})
    assert candidate["shares"] == 0
    assert candidate["indicative_target_shares"] % 100 == 0
    assert candidate["execution_status"] == "INDICATIVE_REVIEW_ONLY"
    # ``strategies`` is reserved for the three point-in-time strategy triggers;
    # weak-regime leader selection is a route, not a fourth strategy.
    assert candidate["strategies"] == []


def test_weak_unavailable_requested_symbol_suppresses_all_buys() -> None:
    decision = DeploymentDecision(
        name="positive_momentum_hold",
        boundary=AS_OF,
        reason="test",
        regime=RegimeEvidence(as_of=AS_OF, regime="weak", observations=()),
        leaders=LeaderSelection(
            as_of=AS_OF,
            requested_symbols=("300394", "300502"),
            observed_symbols=1,
            selected_symbols=("300502",),
            selected_returns=(0.1,),
            unavailable_symbols=("300394",),
        ),
    )
    result, calls = _run_with_market(
        snapshot=_snapshot(),
        symbols={"300394": "缺失候选", "300502": "候选"},
        decision=decision,
        frames={"300394": _frame(), "300502": _frame()},
    )

    assert calls == Counter({"300394": 1, "300502": 1})
    assert not any(item["action"] == "BUY_CANDIDATE" for item in result["actions"])
    assert result["data_complete"] is False
    assert result["unavailable_symbols"] == ["300394"]
    assert result["buys_suppressed"] is True
    assert result["buy_suppression_reasons"] == [
        "LEADER_DATA_UNAVAILABLE:300394"
    ]


def test_weak_unavailable_symbols_remain_visible_when_no_leader_is_selected() -> None:
    decision = DeploymentDecision(
        name="cash_preservation",
        boundary=AS_OF,
        reason="no observable leader",
        regime=RegimeEvidence(as_of=AS_OF, regime="weak", observations=()),
        leaders=LeaderSelection(
            as_of=AS_OF,
            requested_symbols=("300394", "300502"),
            observed_symbols=1,
            selected_symbols=(),
            selected_returns=(),
            unavailable_symbols=("300394",),
        ),
    )
    result, calls = _run_with_market(
        snapshot=_snapshot(),
        symbols={"300394": "缺失候选", "300502": "无信号候选"},
        decision=decision,
        frames={"300394": _frame(), "300502": _frame()},
    )

    assert calls == Counter({"300394": 1, "300502": 1})
    assert result["data_complete"] is False
    assert result["unavailable_symbols"] == ["300394"]
    assert result["buy_suppression_reasons"] == [
        "LEADER_DATA_UNAVAILABLE:300394"
    ]


def test_real_weak_route_reuses_one_frozen_frame_per_symbol() -> None:
    engine = account_scan.AccountSignalEngine(
        cache_dir="unused",
        regime_data_dir="unused",
    )
    calls: Counter[str] = Counter()
    frame = _frame()
    frame.loc[:, "open"] = range(100, 200)
    frame.loc[:, "close"] = range(100, 200)
    frame.loc[:, "high"] = range(101, 201)
    frame.loc[:, "low"] = range(99, 199)

    def _load(code: str, as_of: str) -> pd.DataFrame:
        calls[code] += 1
        return frame

    with (
        patch.object(
            account_scan.data_contracts,
            "refresh_regime_indices",
            return_value={},
        ),
        patch.object(
            replay_module,
            "boundary_route",
            return_value=RegimeRoute.WEAK,
        ),
        patch.object(
            replay_module,
            "detect_regime",
            return_value=RegimeEvidence(
                as_of=AS_OF,
                regime="weak",
                observations=(),
            ),
        ),
        patch.object(engine, "_frame", side_effect=_load),
        patch.object(
            account_scan.BacktestEngine,
            "config_for_symbol",
            return_value={},
        ),
        patch.object(
            account_scan.Indicators,
            "compute_all",
            side_effect=_indicators,
        ),
    ):
        result = engine.run(
            _snapshot(),
            {"300308": "候选 A", "300502": "候选 B"},
            as_of=AS_OF,
        )

    assert result["data_complete"] is True
    assert any(item["action"] == "BUY_CANDIDATE" for item in result["actions"])
    assert calls == Counter(
        {
            "300308": 1,
            "300394": 1,
            "300502": 1,
            "603986": 1,
            "688008": 1,
        }
    )


def test_entry_before_loaded_window_without_peak_disables_peak_stop() -> None:
    result, _ = _run_with_market(
        snapshot=_holding_snapshot(
            cash=0.0,
            entry_date="2025-01-02",
            highest_close=None,
        ),
        symbols={"300308": "持仓"},
        decision=_decision("frozen_trend_engine"),
        frames={"300308": _frame(close=100.0, peak=200.0)},
    )

    held = next(item for item in result["actions"] if item["symbol"] == "300308")
    assert held["action"] == "HOLD"
    assert held["protective_stop"] == 85.0
    assert held["peak_close"] == 100.0
    assert held["peak_evidence_status"] == "PEAK_EVIDENCE_INCOMPLETE"
    assert "PEAK_EVIDENCE_INCOMPLETE" in held["reason"]


def test_entry_after_latest_market_evidence_is_data_error() -> None:
    result, _ = _run_with_market(
        snapshot=_holding_snapshot(
            cash=0.0,
            entry_date=AS_OF,
            highest_close=200.0,
        ),
        symbols={"300308": "持仓"},
        decision=_decision("frozen_trend_engine"),
        frames={"300308": _frame(end="2026-01-30")},
    )

    held = next(item for item in result["actions"] if item["symbol"] == "300308")
    assert held["action"] == "DATA_ERROR"
    assert held["execution_status"] == "DATA_UNAVAILABLE"
    assert "entry_date is later than market evidence_date=2026-01-30" in held["reason"]
    assert "peak_close" not in held


def test_snapshot_highest_close_completes_old_entry_peak_evidence() -> None:
    result, _ = _run_with_market(
        snapshot=_holding_snapshot(
            cash=0.0,
            entry_date="2025-01-02",
            highest_close=200.0,
        ),
        symbols={"300308": "持仓"},
        decision=_decision("frozen_trend_engine"),
        frames={"300308": _frame(close=100.0)},
    )

    held = next(item for item in result["actions"] if item["symbol"] == "300308")
    assert held["action"] == "SELL"
    assert held["peak_close"] == 200.0
    assert held["peak_evidence_status"] == "COMPLETE"


def test_peak_ignores_prices_before_entry_date() -> None:
    frame = _frame(close=100.0)
    frame.loc[frame.index[-40], "close"] = 500.0
    entry_date = str(frame.index[-20].date())
    frame.loc[frame.index[-10], "close"] = 110.0

    result, _ = _run_with_market(
        snapshot=_holding_snapshot(
            cash=0.0,
            entry_date=entry_date,
            highest_close=None,
        ),
        symbols={"300308": "持仓"},
        decision=_decision("frozen_trend_engine"),
        frames={"300308": frame},
    )

    held = next(item for item in result["actions"] if item["symbol"] == "300308")
    assert held["peak_close"] == 110.0
    assert held["peak_evidence_status"] == "COMPLETE"
