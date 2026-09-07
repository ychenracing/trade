from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.engine.replay import ProductionRouteController
from quantfusion.engine.universe import BacktestEngine
from quantfusion.risk.managers import RecoverableDrawdownRiskManager


class _Sleeve:
    def __init__(self, name: str, cash: float, *, regime: str = "CHOPPY") -> None:
        self.sleeve_name = name
        self.cash = cash
        self._regime_state = regime
        self.positions = {}
        self.risk = RecoverableDrawdownRiskManager(
            default_engine_config(), PortfolioPolicy()
        )
        self.risk.peak_assets = cash * 1.30
        self.risk.lifetime_peak_assets = cash * 1.30
        self.risk.daily_start_assets = cash * 1.20
        self.equity_curve = [
            {
                "date": "2026-01-05",
                "assets": cash,
                "cash": cash,
                "position_value": 0.0,
            }
        ]

    def _execution_mark_prices(self, _data_map: dict, _date: pd.Timestamp) -> dict:
        return {}

    def _total_assets_at_prices(self, _prices: dict) -> float:
        return float(self.cash)


def _state(sleeve: _Sleeve) -> SimpleNamespace:
    return SimpleNamespace(sleeve=sleeve, data_map={})


def _drawdown(peak: float, assets: float) -> float:
    return (peak - assets) / peak if peak > 0 else 0.0


def test_dynamic_idle_cash_reweight_is_drawdown_neutral() -> None:
    sleeves = [
        _Sleeve("fast", 1_500_000.0),
        _Sleeve("base", 750_000.0),
        _Sleeve("slow", 750_000.0),
    ]
    states = [_state(sleeve) for sleeve in sleeves]
    before = [
        _drawdown(sleeve.risk.lifetime_peak_assets, sleeve.cash)
        for sleeve in sleeves
    ]

    coordinator = BacktestEngine()
    coordinator._last_sleeve_weight_regime = "TREND"
    coordinator._sleeve_weight_events = []
    coordinator._rebalance_free_sleeve_cash(states, pd.Timestamp("2026-01-06"))

    after = [
        _drawdown(sleeve.risk.lifetime_peak_assets, sleeve.cash)
        for sleeve in sleeves
    ]
    assert after == before
    assert sleeves[0].risk.check_portfolio_risk(
        sleeves[0].cash, "2026-01-06"
    ) is None
    assert sleeves[0].risk.terminal_lock is False


def test_route_cash_migration_rebases_risk_and_latest_sleeve_equity() -> None:
    sleeves = [
        _Sleeve("fast", 1_000_000.0, regime="TREND"),
        _Sleeve("base", 1_000_000.0, regime="TREND"),
        _Sleeve("slow", 1_000_000.0, regime="TREND"),
    ]
    states = [_state(sleeve) for sleeve in sleeves]
    before = [
        _drawdown(sleeve.risk.lifetime_peak_assets, sleeve.cash)
        for sleeve in sleeves
    ]

    ProductionRouteController._shift_free_cash(states, (1.0, 0.0, 0.0))

    assert [sleeve.cash for sleeve in sleeves] == [3_000_000.0, 0.0, 0.0]
    assert sleeves[0].risk.lifetime_peak_assets == 3_900_000.0
    assert _drawdown(
        sleeves[0].risk.lifetime_peak_assets, sleeves[0].cash
    ) == before[0]
    assert sleeves[0].equity_curve[-1] == {
        "date": "2026-01-05",
        "assets": 3_000_000.0,
        "cash": 3_000_000.0,
        "position_value": 0.0,
    }
    for sleeve in sleeves[1:]:
        assert sleeve.risk.lifetime_peak_assets == 0.0
        assert sleeve.equity_curve[-1]["assets"] == 0.0
        assert sleeve.equity_curve[-1]["cash"] == 0.0
        assert sleeve.equity_curve[-1]["position_value"] == 0.0
