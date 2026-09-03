"""Current 17-symbol / 958-scenario formal stress contract."""

from collections import Counter

from quantfusion.application import stress_scenarios
from quantfusion.config import daily
from quantfusion.config.universe import SYMBOL_NAMES


EXPECTED_SYMBOLS = (
    "300308", "300502", "300394", "688256", "603986",
    "688072", "688300", "300054", "688361", "002409",
    "688498", "688120", "002384", "688082", "300604",
    "601869", "300408",
)


def test_daily_and_formal_stress_share_current_ordered_universe() -> None:
    assert tuple(SYMBOL_NAMES) == EXPECTED_SYMBOLS
    assert tuple(daily.SYMBOLS) == EXPECTED_SYMBOLS


def test_current_formal_plan_has_exact_958_family_counts() -> None:
    scenarios = stress_scenarios._multi_seed_scenarios(
        random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
        permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
        seeds=stress_scenarios.DEFAULT_SEEDS,
    )
    assert len(scenarios) == 958
    assert len({item["scenario_id"] for item in scenarios}) == 958
    assert Counter(item["scenario_type"] for item in scenarios) == {
        "prefix": 17,
        "leave_one_out": 17,
        "add_one": 24,
        "random_subset": 750,
        "permutation": 150,
    }
