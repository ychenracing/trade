"""Contracts for random-subset sampling after the 17-symbol migration."""

from quantfusion.application import stress_scenarios


def test_random_subset_sizes_keep_fifty_unique_samples_without_loo_overlap() -> None:
    scenarios = stress_scenarios._scenarios(
        random_samples=50,
        permutation_samples=1,
        seed=20260807,
    )
    random_sizes = {
        int(item["sample_size"])
        for item in scenarios
        if item["scenario_type"] == "random_subset"
    }

    assert random_sizes == {3, 5, 8, 12, 15}
    assert sum(item["scenario_type"] == "random_subset" for item in scenarios) == 250
