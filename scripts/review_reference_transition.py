"""Read-only review of a corrected prefix comparator, never an acceptance route.

The five-percent screen is cumulative against immutable original evidence. It
neither changes a frozen gate nor qualifies a reference, and never clips an
observed return to make the proposed target look acceptable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from quantfusion.application import native_joint, stress_artifacts, stress_metrics, stress_scenarios
from quantfusion.application.c6_contract import canonical_payload_hash
from quantfusion.config.paths import PROJECT_ROOT

MAX_REVIEW_CHANGE = .05
INCUMBENT_PAYLOAD_SHA256 = 'ba9f63ab834c49ba00ae9c724f3ab37beec38f9f545205f94e5651e8a43debdb'


def _references() -> tuple[dict, dict, list[dict]]:
    original = native_joint.load_original_reference()
    incumbent = json.loads((PROJECT_ROOT / 'artifacts/validation/universe_stress.json').read_text())
    if canonical_payload_hash(incumbent) != INCUMBENT_PAYLOAD_SHA256:
        raise ValueError('Review requires the immutable original incumbent, not a later target')
    plan = stress_scenarios._multi_seed_scenarios(
        random_samples=50, permutation_samples=50, seeds=stress_scenarios.DEFAULT_SEEDS)
    if stress_scenarios._scenario_signature(plan) != original['scenario_signature']:
        raise ValueError('Review scenario plan differs from the original freeze')
    return original, incumbent, plan


def _bound_prefixes(evidence: dict, original: dict, plan: list[dict]) -> list[dict]:
    identity = evidence.get('identity')
    if not isinstance(identity, dict):
        raise ValueError('Missing run identity')
    for name, length in (('source_revision', 40), ('source_fingerprint', 64)):
        if re.fullmatch('[0-9a-f]{' + str(length) + '}', str(identity.get(name, ''))) is None:
            raise ValueError(f'Invalid run identity: {name}')
    if (identity.get('data_fingerprint') != original['data_fingerprint']
            or identity.get('cfg_overrides') != {}
            or identity.get('allow_publication') is not False
            or identity.get('diagnostic_noncanonical') is not True):
        raise ValueError('Reference data/configuration/publication identity differs')
    for name in ('start_date', 'end_date', 'initial_capital', 'engine', 'deployment_policy'):
        if name in identity and identity[name] != original[name]:
            raise ValueError(f'Run identity differs: {name}')
    rows = evidence.get('results')
    if not isinstance(rows, list):
        raise ValueError('Evidence must contain complete prefix results')
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Evidence result must be an object')
        value = row.get('total_return')
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or 1 + value <= 0):
            raise ValueError('Result needs a finite number and strictly positive wealth')
    indexed = stress_artifacts._validated_checkpoint_results({'results': rows}, plan)
    for row in rows:
        if (isinstance(row.get('symbol_count'), bool)
                or row.get('symbol_count') != len(row['symbols'])
                or not -1 <= row['max_drawdown'] <= 0):
            raise ValueError('Invalid symbol count or signed drawdown metric')
    prefixes = [scenario['scenario_id'] for scenario in plan if scenario['scenario_type'] == 'prefix']
    if any(sid not in indexed for sid in prefixes):
        raise ValueError('Evidence must contain all complete prefix results')
    declared = identity.get('scenario_ids')
    if (not isinstance(declared, list) or len(declared) != len(set(declared))
            or set(declared) != set(indexed)):
        raise ValueError('Run identity scenario IDs differ from complete results')
    return [indexed[sid] for sid in prefixes]


def load_run(directory: Path) -> dict:
    """Verify retained row/byte bindings; never unpickle or certify raw economics."""
    identity = json.loads((directory / 'identity.json').read_text())
    evidence = json.loads((directory / 'results.json').read_text())
    if evidence.get('identity') != identity:
        raise ValueError('Combined results and per-run identity differ')
    original, _, plan = _references()
    _bound_prefixes(evidence, original, plan)
    raw_hashes = {}
    for row in evidence['results']:
        sid = row['scenario_id']  # Validated against the formal plan before constructing paths.
        record = json.loads((directory / (sid + '.json')).read_text())
        if (record.get('source_fingerprint') != identity['source_fingerprint']
                or record.get('result') != row):
            raise ValueError(f'Per-row source/result identity differs: {sid}')
        actual = hashlib.sha256((directory / (sid + '.pkl')).read_bytes()).hexdigest()
        if actual != record.get('raw_sha256'):
            raise ValueError(f'Raw evidence hash differs: {sid}')
        raw_hashes[sid] = actual
    return {**evidence, 'verified_raw_hashes': raw_hashes}


def review(reference: dict, *, candidate: dict | None = None,
           change_budget: float = MAX_REVIEW_CHANGE) -> dict[str, Any]:
    """Measure a proposal without changing any frozen reference or publisher."""
    if (isinstance(change_budget, bool) or not isinstance(change_budget, (int, float))
            or not math.isfinite(change_budget) or not 0 <= change_budget <= MAX_REVIEW_CHANGE):
        raise ValueError('Review change budget must be finite and between zero and five percent')
    original, incumbent, plan = _references()
    proposed = _bound_prefixes(reference, original, plan)
    original_by_id = {row['scenario_id']: row for row in original['results']}
    incumbent_by_id = {row['scenario_id']: row for row in incumbent['results']}
    production_id = proposed[-1]['scenario_id']
    old_lower, new_lower, rows = 0., 0., []
    for count, item in enumerate(proposed, 1):
        sid = item['scenario_id']
        ratio = .99 if sid == 'prefix-05' else .95
        old_floor = ratio * (1 + original_by_id[sid]['total_return'])
        requested_floor = ratio * (1 + item['total_return'])
        # The production use-case cannot lose its existing original floor.
        new_floor = max(old_floor, requested_floor) if sid == production_id else requested_floor
        incumbent_floor = .99 * (1 + incumbent_by_id[sid]['total_return'])
        old_own, new_own = max(old_floor, incumbent_floor), max(new_floor, incumbent_floor)
        adjacent_ratio = .90 if count == 10 else .70
        old_lower = max(old_own, old_lower * adjacent_ratio)
        new_lower = max(new_own, new_lower * adjacent_ratio)
        rows.append({
            'scenario_id': sid, 'old_reference_floor': old_floor,
            'requested_reference_floor': requested_floor, 'proposed_reference_floor': new_floor,
            'reference_floor_change': requested_floor / old_floor - 1,
            'incumbent_floor': incumbent_floor, 'old_own_floor': old_own, 'proposed_own_floor': new_own,
            'old_joint_infimum': old_lower, 'proposed_joint_infimum': new_lower,
            'joint_infimum_change': new_lower / old_lower - 1,
            'strict_lower_bound': count == 10,
            'production_floor_retained': sid == production_id,
        })
    material = [row['scenario_id'] for row in rows if any(
        abs(row[field]) > change_budget + 1e-12
        for field in ('reference_floor_change', 'joint_infimum_change'))]
    result: dict[str, Any] = {
        'kind': 'reference_transition_impact_review', 'canonical': False,
        'activation_allowed': False,
        'status': 'MATERIAL_TARGET_CHANGE' if material else 'REFERENCE_QUALIFICATION_REQUIRED',
        'original_reference_payload_sha256': canonical_payload_hash(original),
        'incumbent_payload_sha256': canonical_payload_hash(incumbent),
        'reference_identity': dict(reference['identity']),
        'reference_results_payload_sha256': canonical_payload_hash(reference['results']),
        'verified_raw_hashes': reference.get('verified_raw_hashes'),
        'modest_change_screen': {'budget': change_budget, 'anchor': 'immutable_original_freeze',
                                 'within_budget': not material, 'material_prefixes': material},
        'rows': rows,
        'unchanged': ['18% drawdown', 'random bucket P90<=160', 'all buckets<=200',
                      'all incumbent protections', 'production17 original floor',
                      'ordinary adjacent change>=-30%', '09->10 change strictly>-10%',
                      'original worst-return/add-one protections', 'correctness and execution'],
        'qualification': 'NOT_ESTABLISHED: source/byte bindings and prefix metrics do not certify '
                         'locked runtime, all required correctness, full958 or deployment qualification.',
        'limits': 'Joint infima are necessary, not sufficient. The09->10 nominal infimum may be open; '
                  'use the existing strict floating-point formula on actual candidate predecessors. '
                  'No observed return is clipped. This report is never a canonical reference or PASS.',
    }
    if candidate is not None:
        actual = _bound_prefixes(candidate, original, plan)
        violations = {'old_own_floors': [], 'proposed_own_floors': []}
        for row, outcome in zip(rows, actual, strict=True):
            wealth = 1 + outcome['total_return']
            for key, floor in (('old_own_floors', row['old_own_floor']),
                               ('proposed_own_floors', row['proposed_own_floor'])):
                if wealth < floor - 1e-12:
                    violations[key].append(row['scenario_id'])
        result['candidate_identity'] = dict(candidate['identity'])
        result['candidate_diagnostic_checks'] = {
            'wealth_floor_violations': violations,
            'retained_robustness': stress_metrics._retained_robustness_hard_gates(actual),
            'prefix_max_drawdown': min(row['max_drawdown'] for row in actual),
            'prefix_max_buckets': max(row['date_symbol_side_count'] for row in actual),
            'formal_random_p90': None, 'formal_acceptance': 'NOT_EVALUATED',
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-run', required=True, type=Path)
    parser.add_argument('--candidate-run', type=Path)
    args = parser.parse_args()
    result = review(load_run(args.reference_run),
                    candidate=load_run(args.candidate_run) if args.candidate_run else None)
    # stdout only: no write route to canonical artifacts or frozen contracts.
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
