"""Stable application-facing namespace for backtest engine entry points."""

from quantfusion.data.providers import DataFetcher as DataFetcher
from quantfusion.domain.models import TradeRecord as TradeRecord
from quantfusion.engine.core import CoreBacktestEngine
from quantfusion.engine.universe import BacktestEngine as BacktestEngine
from quantfusion.config.portfolio import PortfolioPolicy as PortfolioPolicy


def get_symbol_group(code: str, default: str = "default") -> str:
    """Return the public engine routing group for a symbol."""
    return CoreBacktestEngine.get_symbol_group(code, default)


def get_symbol_profile(code: str, default: str = "default") -> str:
    """Return the public engine routing profile for a symbol."""
    return CoreBacktestEngine.get_symbol_profile(code, default)


__all__ = [
    "BacktestEngine",
    "DataFetcher",
    "PortfolioPolicy",
    "TradeRecord",
    "get_symbol_group",
    "get_symbol_profile",
]
