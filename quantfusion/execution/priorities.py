"""Stable deterministic execution priorities."""

from quantfusion.config.universe import SYMBOL_NAMES

EXECUTION_PRIORITY = {
    code: rank for rank, code in enumerate(SYMBOL_NAMES)
}
