"""Deterministic stress-scenario construction and selection."""

from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Any

from quantfusion.application import stress_metrics
from quantfusion.config.universe import SYMBOL_NAMES

ORDERED_CODES = tuple(SYMBOL_NAMES)
DEFAULT_RANDOM_SAMPLES = 50
DEFAULT_PERMUTATION_SAMPLES = 50
DEFAULT_SEEDS = (20260807, 20260817, 20260827)


def _scenarios(
    *,
    random_samples: int,
    permutation_samples: int,
    seed: int,
    include_fixed: bool = True,
) -> list[dict[str, Any]]:
    """Build one deterministic seed block; fixed scenarios are optional."""
    if random_samples < 1 or permutation_samples < 1:
        raise ValueError("sample counts must be positive")
    smallest_capacity = min(
        math.comb(len(ORDERED_CODES), size) for size in (3, 5, 8, 12, 16)
    )
    if random_samples > smallest_capacity:
        raise ValueError(
            f"random_samples exceeds unique subset capacity {smallest_capacity}"
        )
    if permutation_samples > math.factorial(len(ORDERED_CODES)):
        raise ValueError("permutation_samples exceeds unique ordering capacity")
    rng = random.Random(seed)
    scenarios: list[dict[str, Any]] = []
    if include_fixed:
        for count in range(1, len(ORDERED_CODES) + 1):
            scenarios.append(
                {
                    "scenario_id": f"prefix-{count:02d}",
                    "scenario_type": "prefix",
                    "symbols": list(ORDERED_CODES[:count]),
                }
            )
        for omitted in ORDERED_CODES:
            scenarios.append(
                {
                    "scenario_id": f"leave-one-out-{omitted}",
                    "scenario_type": "leave_one_out",
                    "omitted_symbol": omitted,
                    "symbols": [code for code in ORDERED_CODES if code != omitted],
                }
            )
        for base_size in (5, 9, 13):
            base = ORDERED_CODES[:base_size]
            for added in ORDERED_CODES[base_size:]:
                scenarios.append(
                    {
                        "scenario_id": f"add-one-{base_size:02d}-{added}",
                        "scenario_type": "add_one",
                        "base_size": base_size,
                        "added_symbol": added,
                        "symbols": [*base, added],
                    }
                )
    for size in (3, 5, 8, 12, 16):
        seen: set[tuple[str, ...]] = set()
        while len(seen) < random_samples:
            seen.add(tuple(sorted(rng.sample(ORDERED_CODES, size))))
        for index, subset in enumerate(sorted(seen), start=1):
            scenarios.append(
                {
                    "scenario_id": f"random-{seed}-{size:02d}-{index:03d}",
                    "scenario_type": "random_subset",
                    "seed": seed,
                    "sample_size": size,
                    "symbols": list(subset),
                }
            )
    permutations: set[tuple[str, ...]] = set()
    while len(permutations) < permutation_samples:
        sample = list(ORDERED_CODES)
        rng.shuffle(sample)
        permutations.add(tuple(sample))
    for index, ordering in enumerate(sorted(permutations), start=1):
        scenarios.append(
            {
                "scenario_id": f"permutation-{seed}-{index:03d}",
                "scenario_type": "permutation",
                "seed": seed,
                "symbols": list(ordering),
            }
        )
    return scenarios


def _multi_seed_scenarios(
    *, random_samples: int, permutation_samples: int, seeds: tuple[int, ...]
) -> list[dict[str, Any]]:
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("stress seeds must be non-empty and unique")
    scenarios: list[dict[str, Any]] = []
    for index, seed in enumerate(seeds):
        scenarios.extend(
            _scenarios(
                random_samples=random_samples,
                permutation_samples=permutation_samples,
                seed=seed,
                include_fixed=index == 0,
            )
        )
    return scenarios


def is_canonical_scenario_plan(scenarios: list[dict[str, Any]]) -> bool:
    """Return whether scenarios exactly match the ordered formal plan."""
    return scenarios == _multi_seed_scenarios(
        random_samples=DEFAULT_RANDOM_SAMPLES,
        permutation_samples=DEFAULT_PERMUTATION_SAMPLES,
        seeds=DEFAULT_SEEDS,
    )


def _scenario_signature(scenarios: list[dict[str, Any]]) -> str:
    payload = {
        "engine": stress_metrics.ENGINE,
        "deployment_policy": stress_metrics.DEPLOYMENT_POLICY,
        "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
        "start_date": stress_metrics.START_DATE,
        "end_date": stress_metrics.END_DATE,
        "initial_capital": stress_metrics.INITIAL_CAPITAL,
        "ordered_codes": list(ORDERED_CODES),
        "scenarios": scenarios,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def select_scenarios(
    scenarios: list[dict[str, Any]],
    *,
    scenario_id: str | None,
    scenario_type: str | None,
    shard_index: int | None,
    shard_count: int | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Select diagnostics in formal order and report formal completeness."""
    if (shard_index is None) != (shard_count is None):
        raise ValueError("shard-index and shard-count must be provided together")
    if shard_count is not None and (
        shard_count < 1
        or shard_index is None
        or shard_index < 0
        or shard_index >= shard_count
    ):
        raise ValueError("shard-count must be positive and shard-index in range")

    selected = [
        scenario
        for index, scenario in enumerate(scenarios)
        if (scenario_id is None or scenario["scenario_id"] == scenario_id)
        and (scenario_type is None or scenario["scenario_type"] == scenario_type)
        and (shard_count is None or index % shard_count == shard_index)
    ]
    if not selected:
        raise ValueError("Stress selection matched no scenarios")
    selector_used = any(
        value is not None
        for value in (scenario_id, scenario_type, shard_index, shard_count)
    )
    return selected, not selector_used and is_canonical_scenario_plan(scenarios)
