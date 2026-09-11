"""Synthetic formal publication gates for the exact owner-approved AB5 release."""
from __future__ import annotations

from copy import deepcopy

import pytest

from quantfusion.application import c6_release_acceptance as release
from quantfusion.application import stress, stress_artifacts, stress_scenarios
from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR


def _gates():
    return {
        "absolute_hard_gates": {
            "checks": {"all_scenarios_max_drawdown_at_most_18pct": False,
                       "random_p90_date_symbol_side_buckets_at_most_160": False,
                       "all_date_symbol_side_buckets_at_most_200": False},
            "observed": {"all_worst_drawdown": -0.2110621724651241,
                         "random_p90_date_symbol_side_buckets": 184.0,
                         "all_worst_date_symbol_side_buckets": 230},
        },
        "retained_robustness_hard_gates": {
            "checks": {"prefix_9_to_10_wealth_above_minus_10pct": False,
                       "worst_adjacent_wealth_at_least_minus_30pct": False},
            "observed": {"prefix_9_to_10_wealth_change": -0.114,
                         "worst_adjacent_wealth_change": -0.4557191766929257},
        },
        "initial_baseline_gates": {
            "checks": {"prefix_05_wealth_at_least_99pct": False,
                       "other_prefix_wealth_at_least_95pct": False,
                       "worst_total_return_not_worse_by_0_02": False,
                       "worst_add_one_diagnostic_not_worse_by_0_03": False},
            "observed": {"prefix_05_wealth_ratio": 0.723166270514689,
                         "other_prefix_wealth_ratio_min": 0.33223093416649163,
                         "worst_total_return": 0.078, "reference_worst_total_return": 0.1,
                         "worst_add_one_wealth_change": -0.034,
                         "reference_worst_add_one_wealth_change": 0.0},
        },
        "promotion_gates": {"permutation_invariance": {"invariant": True}},
    }


def test_formal_profile_applies_all_and_only_authorized_limits():
    raw = _gates()
    before = deepcopy(raw)
    assessed = release.release_formal_assessment(raw)
    assert raw == before
    assert assessed["passed"] is True
    assert len(assessed["predicate_results"]) == 10
    assert sum(row["exception_applied"] is not None for row in assessed["predicate_results"]) == 4
    assert assessed["acceptance_revision"] == release.ACCEPTANCE_REVISION


@pytest.mark.parametrize(("family", "field", "value"), [
    ("absolute_hard_gates", "all_worst_drawdown", -0.212),
    ("absolute_hard_gates", "random_p90_date_symbol_side_buckets", 184.01),
    ("absolute_hard_gates", "all_worst_date_symbol_side_buckets", 231),
    ("retained_robustness_hard_gates", "prefix_9_to_10_wealth_change", -0.115),
    ("retained_robustness_hard_gates", "worst_adjacent_wealth_change", -0.456),
    ("initial_baseline_gates", "prefix_05_wealth_ratio", 0.723),
    ("initial_baseline_gates", "other_prefix_wealth_ratio_min", 0.332),
    ("initial_baseline_gates", "worst_total_return", 0.076),
    ("initial_baseline_gates", "worst_add_one_wealth_change", -0.035),
])
def test_formal_profile_still_rejects_a_result_outside_authorized_limits(family, field, value):
    raw = _gates()
    raw[family]["observed"][field] = value
    assert release.release_formal_assessment(raw)["passed"] is False


def test_formal_profile_does_not_waive_permutation_or_missing_gates():
    raw = _gates()
    raw["promotion_gates"]["permutation_invariance"]["invariant"] = False
    assert release.release_formal_assessment(raw)["passed"] is False
    del raw["absolute_hard_gates"]["checks"]["all_date_symbol_side_buckets_at_most_200"]
    with pytest.raises(ValueError, match="gate coverage"):
        release.release_formal_assessment(raw)


def test_candidate_identity_is_bound_into_formal_checkpoint_signature():
    plan = stress_scenarios._multi_seed_scenarios(random_samples=50, permutation_samples=50, seeds=(20260807, 20260817, 20260827))
    args = (plan, MARKET_DATA_DIR, REGIME_DATA_DIR)
    base = stress_artifacts._build_provenance(*args, source_revision="a" * 40, candidate_id="C6-Base")
    ab5 = stress_artifacts._build_provenance(*args, source_revision="a" * 40, candidate_id="C6-Base+AB5")
    assert base["run_signature"] != ab5["run_signature"]
    assert ab5["candidate_id"] == "C6-Base+AB5"
    with pytest.raises(ValueError, match="provenance|signature"):
        stress_artifacts._validated_checkpoint(
            {"signature":base["run_signature"], "provenance":base, "results":[]},
            plan, signature=ab5["run_signature"], provenance=ab5, diagnostic_selection=None,
        )


def test_release_cli_requires_explicit_named_acceptance():
    args = stress.build_argument_parser().parse_args([
        "--source-revision", "a" * 40, "--candidate-id", "C6-Base+AB5",
        "--ab5-release-acceptance",
    ])
    assert args.ab5_release_acceptance is True


def test_release_source_rejects_a_mislabelled_actual_checkout():
    with pytest.raises(ValueError, match="actual checkout"):
        release.verify_ab5_release_source("0" * 40)


def test_release_reference_must_be_the_exact_recorded_reference():
    with pytest.raises(ValueError, match="reference"):
        stress_artifacts.validate_ab5_release_request(
            {"candidate_id":"C6-Base+AB5", "data_fingerprint":release.AB5_DATA_FINGERPRINT},
            {"results":[]},
        )


def test_initial_release_publisher_preserves_native_gates_and_refuses_unwaived_failure(tmp_path, monkeypatch):
    """Synthetic publication glue; only source/L2-proof boundaries and output are isolated."""
    import json
    from quantfusion.application import stress_metrics
    from quantfusion.config.paths import PROJECT_ROOT

    plan = stress_scenarios._multi_seed_scenarios(random_samples=50, permutation_samples=50,
                                                seeds=(20260807, 20260817, 20260827))
    reference = json.loads((PROJECT_ROOT / 'artifacts/validation/candidates/stress-86fd22448b9aad9d5e6194c0c065c40d56d7bddd-rejected.json').read_text())
    total_return = max(row['total_return'] for row in reference['results']) + 1
    rows = [{**scenario, 'symbol_count':len(scenario['symbols']), 'total_return':total_return,
             'max_drawdown':-0.2110621724651241, 'sharpe':1., 'calmar':1., 'total_trades':230,
             'sleeve_fill_count':230, 'date_symbol_side_count':184 if scenario['scenario_type']=='random_subset' else 230,
             'max_concurrent_symbols':len(scenario['symbols']), 'terminal_risk_lock':True,
             'reason_attribution':{k:230 if k=='initial_entry' else 0 for k in stress_metrics.ATTRIBUTION_CATEGORIES},
             'deployment_policy':'production_daily_replay'} for scenario in plan]
    provenance = stress_artifacts._build_provenance(plan, MARKET_DATA_DIR, REGIME_DATA_DIR,
                                                  source_revision='a'*40, candidate_id='C6-Base+AB5')
    def artifacts():
        prefix = {**provenance, 'results':[row for row in rows if row['scenario_type']=='prefix']}
        universe = {**provenance, 'results':rows, 'trade_count_semantics':'trade_records',
                    'seeds':list(stress_scenarios.DEFAULT_SEEDS),
                    'absolute_hard_gates':stress_metrics._absolute_hard_gates(rows),
                    'retained_robustness_hard_gates':stress_metrics._retained_robustness_hard_gates(rows),
                    'robustness_diagnostics':stress_metrics._robustness_diagnostics(rows),
                    'promotion_gates':stress_metrics._promotion_gates(rows,None),
                    'initial_baseline_gates':stress_metrics._initial_baseline_gates(rows, reference)}
        return prefix, universe
    monkeypatch.setattr(stress_artifacts, 'VALIDATION_ARTIFACT_DIR', tmp_path)
    monkeypatch.setattr(release, 'verify_ab5_release_source', lambda revision: {'test_source':revision})
    monkeypatch.setattr(stress_artifacts, 'validate_release_l2_evidence', lambda *a, **k: {'test_l2':'separately validated'})
    kwargs = dict(scenarios=plan, provenance=provenance, incumbent=None, formal_plan_complete=True,
                  establish_initial_baseline=True, initial_baseline_reference=reference)
    prefix, universe = artifacts()
    before = deepcopy(universe)
    assert stress_artifacts._publish_formal_artifacts(prefix, universe, **kwargs) is False
    assert stress_artifacts._publish_formal_artifacts(prefix, universe, ab5_release_acceptance=True, **kwargs) is True
    saved = json.loads((tmp_path/'universe_stress.json').read_text())
    assert universe == before
    assert saved['absolute_hard_gates'] == before['absolute_hard_gates']
    assert saved['absolute_hard_gates']['passed'] is False
    assert saved['release_acceptance']['assessment']['passed'] is True
    assert saved['acceptance_status'] == 'accepted' and saved['canonical'] is True
    # A genuinely worse result is not covered; do not replace the canonical files.
    canonical = (tmp_path/'universe_stress.json').read_bytes()
    rows[0]['max_drawdown'] = -.212
    assert stress_artifacts._publish_formal_artifacts(*artifacts(), ab5_release_acceptance=True, **kwargs) is False
    assert (tmp_path/'universe_stress.json').read_bytes() == canonical
    # Corrupting the native inputs remains a validation error, not a waiver.
    prefix, universe = artifacts()
    universe['absolute_hard_gates']['passed'] = True
    with pytest.raises(ValueError, match='absolute hard gates'):
        stress_artifacts._publish_formal_artifacts(prefix, universe, ab5_release_acceptance=True, **kwargs)


def test_release_predicates_reject_empty_duplicate_and_nonboolean_rows():
    from tests.c6_non_economic.test_c6_release_acceptance import _assess, _payload, _predicate

    for payload in (_payload(), _payload(*([_predicate('l1.metrics.finite', True, passed=True)] * 2))):
        with pytest.raises(ValueError, match='nonempty|duplicate'):
            _assess(payload)
    row = _predicate('l1.initial.prefix05_proxy', .8)
    row['passed'] = 1
    with pytest.raises(ValueError, match='boolean'):
        _assess(_payload(row))


def test_release_runner_authenticates_original_d_and_never_relabels_it(tmp_path):
    import json
    import zipfile
    from scripts import c6_ab5_release

    bad = tmp_path/'invalid.zip'
    with zipfile.ZipFile(bad, 'w') as archive:
        archive.writestr('c6-selection.json', json.dumps({'branch':'BASE_SELECTED'}))
    with pytest.raises(ValueError, match='receipt'):
        c6_ab5_release.read_base_receipt(bad)


def test_formal_publication_requires_complete_source_bound_l2_evidence():
    with pytest.raises(ValueError, match='L2 evidence'):
        stress_artifacts.validate_release_l2_evidence({}, source_revision='a'*40, reference={})
