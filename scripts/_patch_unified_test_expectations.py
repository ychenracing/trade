#!/usr/bin/env python3
"""Temporary branch-only migration of deterministic 17-symbol test expectations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quantfusion.application import stress_metrics, stress_scenarios


def replace_optional(text: str, old: str, new: str, *, label: str) -> str:
    """Replace an old literal once, or accept an already-migrated literal."""
    old_count = text.count(old)
    new_count = text.count(new)
    if old_count == 1:
        return text.replace(old, new, 1)
    if old_count == 0 and new_count >= 1:
        return text
    raise SystemExit(
        f"{label}: expected one old literal or an existing new literal; "
        f"old={old_count}, new={new_count}"
    )


def main() -> None:
    path = Path("tests/unit/test_stress_scenarios.py")
    text = path.read_text(encoding="utf-8")

    text = text.replace('"prefix-22"', '"prefix-17"')
    text = replace_optional(
        text,
        "self.assertEqual(len(selected), 39)",
        "self.assertEqual(len(selected), 24)",
        label="add-one selection count",
    )
    text = replace_optional(
        text,
        'self.assertEqual(selected[0]["scenario_id"], "add-one-05-002409")',
        'self.assertEqual(selected[0]["scenario_id"], "add-one-05-688072")',
        label="first add-one scenario",
    )
    text = replace_optional(
        text,
        'self.assertEqual(selected[-1]["scenario_id"], "add-one-13-688082")',
        'self.assertEqual(selected[-1]["scenario_id"], "add-one-13-300408")',
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
    text = replace_optional(
        text,
        old_expected,
        new_expected,
        label="exact add-one definition",
    )

    historical_start = text.index(
        "    def test_rejected_candidate_retains_complete_current_evidence"
    )
    historical_end = text.index(
        "    def test_absolute_hard_gates_keep_current_ceilings_on_side_buckets",
        historical_start,
    )
    historical = text[historical_start:historical_end]
    # The immutable historical 22-symbol artifact really contains 983 unique IDs.
    historical = historical.replace(
        '''        self.assertEqual(
            len({item["scenario_id"] for item in payload["results"]}),
            958,
        )''',
        '''        self.assertEqual(
            len({item["scenario_id"] for item in payload["results"]}),
            983,
        )''',
    )
    text = text[:historical_start] + historical + text[historical_end:]

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

    provenance_start = text.index(
        "    def test_provenance_exposes_independent_source_data_and_scenario_hashes"
    )
    provenance_end = text.index(
        "    def test_checkpoint_requires_trade_and_bucket_counts_separately",
        provenance_start,
    )
    provenance = text[provenance_start:provenance_end]
    lines = provenance.splitlines()
    hex_literals = []
    for line in lines:
        stripped = line.strip().strip('"')
        if len(stripped) == 64 and all(char in "0123456789abcdef" for char in stripped):
            hex_literals.append(stripped)
    if len(hex_literals) != 2:
        raise SystemExit(
            f"provenance test: expected two 64-char literals, found {hex_literals!r}"
        )
    provenance = provenance.replace(hex_literals[0], scenario_signature, 1)
    provenance = provenance.replace(hex_literals[1], run_signature, 1)
    text = text[:provenance_start] + provenance + text[provenance_end:]
    path.write_text(text, encoding="utf-8")

    regression = Path("tests/regression/test_quant_fusion.py")
    regression_text = regression.read_text(encoding="utf-8")
    # Only migrate explicit current-universe labels. Policy stress tests that
    # intentionally probe a 22-name numeric input remain valid and unchanged.
    regression_text = regression_text.replace('"22_symbols"', '"17_symbols"')
    regression_text = regression_text.replace("'22_symbols'", "'17_symbols'")
    regression.write_text(regression_text, encoding="utf-8")


if __name__ == "__main__":
    main()
