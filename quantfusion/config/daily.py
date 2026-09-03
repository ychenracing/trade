"""Daily scan universe and request defaults."""

from quantfusion.config.paths import REGIME_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES

SYMBOLS: dict[str, str] = dict(SYMBOL_NAMES)
START_DATE = "2026-07-01"
INITIAL_CAPITAL = 2_000_000.0
DEFAULT_CACHE_DIR = "data_cache"
DEFAULT_OUTPUT_DIR = "daily_signals"
DEFAULT_REGIME_DATA_DIR = str(REGIME_DATA_DIR)
