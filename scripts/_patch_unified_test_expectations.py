#!/usr/bin/env python3
"""Temporary branch-only patch for intentional 17-symbol test migrations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quantfusion.application import stress_metrics, stress_scenarios


def replace_exact(text: str, old: str, new: str, *, count: int, label: str) -> str:
    actual = text.count(old)
    if actual != count:
        raise SystemExit(f"{label}: expected {count} occurrences, found {actual}")
    return text.replace(old, new)


def main() -> None:
    path = Path("tests/unit/test_stress_scenarios.py")
    text = path.read_text(encoding="utf-8")

    text = replace_exact(
        text,
        '"prefix-22"',
        '"prefix-17"',
        count=2,
        label="mandatory fixed prefix",
    )
    text = replace_exact(
        text,
        "self.assertEqual(len(selected), 39)",
        "self.assertEqual(len(selected), 24)",
        count=1,
        label="add-one selection count",
    )
    text = replace_exact(
        text,
        'self.assertEqual(selected[0]["scenario_id"], "add-one-05-002409")',
        'self.assertEqual(selected[0]["scenario_id"], "add-one-05-688072")',
        count=1,
        label="first add-one scenario",
    )
    text = replace_exact(
        text,
        'self.assertEqual(selected[-1]["scenario_id"], "add-one-13-688082")',
        'self.assertEqual(selected[-1]["scenario_id"], "add-one-13-300408")',
        count=1,
        label="last add-one scenario",
    )

    old_expected = '''                    "added_symbol": "688205",
                    "symbols": [
                        "300308",
                        "300502",
                        "300394",
                        "688008",
                        "603986",
                        "688205",
                    ],'''
    new_expected = '''                    "added_symbol": "688072",
                    "symbols": [
                        "300308",
                        "300502",
                        "300394",
                        "688256",
                        "603986",
                        "688072",
                    ],'''
    text = replace_exact(
        text,
        old_expected,
        new_expected,
        count=1,
        label="exact add-one definition",
    )

    historical_function_start = text.index(
        "    def test_rejected_candidate_retains_complete_current_evidence"
    )
    historical_function_end = text.index(
        "    def test_absolute_hard_gates_keep_current_ceilings_on_side_buckets",
        historical_function_start,
    )
    historical = text[historical_function_start:historical_function_end]
    historical = replace_exact(
        historical,
        '''        self.assertEqual(
            len({item["scenario_id"] for item in payload["results"]}),
            958,
        )''',
        '''        self.assertEqual(
            len({item["scenario_id"] for item in payload["results"]}),
            983,
        )''',
        count=1,
        label="immutable historical artifact unique count",
    )
    text = (
        text[:historical_function_start]
        + historical
        + text[historical_function_end:]
    )

    small_plan = stress_scenarios._multi_seed_scenarios(
        random_samples=1,
        permutation_samples=1,
        seeds=(7,),
    )
    scenario_signature = stress_scenarios._scenario_signature(small_plan)
    run_payload = {
        "stress_contract_version": stress_metrics.STRESS_CONTRACT_VERSION,
        "source_revision": "a" * 40,
        "source_fingerprint": "source-fingerprint",
        "data_fingerprint": "data-fingerprint",
        "scenario_signature": scenario_signature,
    }
    run_signature = hashlib.sha256(
        json.dumps(run_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    text = replace_exact(
        text,
        "aea62b6423a0b3fdc587c4b5cb080b184850c67bc086fa92aaa8b9483b87cee1",
        scenario_signature,
        count=1,
        label="small-plan scenario signature",
    )
    text = replace_exact(
        text,
        "505e3e723957a6ad106ddb8ec94ea19d8b27edcf2567de36809875cd1fbafde3",
        run_signature,
        count=1,
        label="small-plan run signature",
    )
    path.write_text(text, encoding="utf-8")

    regression_path = Path("tests/regression/test_quant_fusion.py")
    regression = regression_path.read_text(encoding="utf-8")
    if "22_symbols" not in regression:
        raise SystemExit("regression test has no 22_symbols references")
    regression = regression.replace("22_symbols", "17_symbols")
    old_policy_contract = '''        self.assertTrue(
            set(policy.regime_symbols).issubset(ESTABLISHED_EXPANSION_CORE)
        )'''
    new_policy_contract = '''        self.assertEqual(
            policy.regime_symbols,
            ("300308", "300502", "300394", "688008", "603986"),
        )
        self.assertFalse(
            set(policy.regime_symbols).issubset(ESTABLISHED_EXPANSION_CORE)
        )'''
    regression = replace_exact(
        regression,
        old_policy_contract,
        new_policy_contract,
        count=1,
        label="independent fixed signal-reference contract",
    )
    regression_path.write_text(regression, encoding="utf-8")


if __name__ == "__main__":
    main()
