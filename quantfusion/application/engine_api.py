"""Stable application-facing namespace for backtest engine entry points."""

from quantfusion.data.providers import DataFetcher as DataFetcher
from quantfusion.domain.models import TradeRecord as TradeRecord
from quantfusion.engine.core import CoreBacktestEngine
from quantfusion.engine.universe import BacktestEngine as BacktestEngine
from quantfusion.config.portfolio import PortfolioPolicy as PortfolioPolicy

_CoreBacktestEngine = CoreBacktestEngine
