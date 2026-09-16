"""Profile-routing contracts for research-only universe members."""

from quantfusion.config import profiles
from quantfusion.config.overlay import SYMBOL_SUB_INDUSTRY


def test_research_only_symbols_reuse_matching_existing_profiles() -> None:
    assert SYMBOL_SUB_INDUSTRY["688825"] == "memory"
    assert SYMBOL_SUB_INDUSTRY["688037"] == "equipment"
    # Correct security identity and risk grouping without retuning the frozen
    # canonical parameter profiles used by the strategy engine.
    assert profiles.SYMBOL_PROFILES["688825"] == "advanced_packaging"
    assert profiles.SYMBOL_PROFILES["688037"] == "test_measurement"
