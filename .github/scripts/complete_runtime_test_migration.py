from pathlib import Path

p = Path('.github/scripts/apply_research_runtime_boundary.py')
text = p.read_text(encoding='utf-8')
marker = '# Architecture contract should not see research identities in production engine files.\n'
if text.count(marker) != 1:
    raise SystemExit('architecture contract marker changed')
block = r"""# Finish research-test migration after the broad mechanical pass.
p, test_text = load("tests/c6_non_economic/test_c6_diagnostics.py")
test_text = one(
    test_text,
    "from quantfusion.research.c6_runtime import runtime_policy_for_intervention\n",
    "from quantfusion.research.c6_runtime import (\n    run_c6_diagnostic,\n    runtime_policy_for_intervention,\n    validate_c6_diagnostic_request,\n)\n",
    "diagnostics research adapter imports",
)
test_text = one(
    test_text,
    "    diagnostic._c6_intervention = 'W1_DATA_MAP_ONLY'\n",
    "    diagnostic._runtime_policy = runtime_policy_for_intervention('W1_DATA_MAP_ONLY')\n",
    "w1 runtime policy",
)
test_text = test_text.replace(
    "ProductionReplayEngine.validate_c6_diagnostic_request(request)",
    "validate_c6_diagnostic_request(request)",
)
old_ablation = '''def test_ablation_map_is_exact_and_production_defaults_full_on() -> None:
    engine = object.__new__(BacktestEngine)
    assert all(engine._c6_feature_enabled(item) for item in ("F0", "F1", "U"))
    expected = {
        "BASELINE": set(), "F0_ONLY": {"F0"}, "F0_F1": {"F0", "F1"},
        "U_ONLY": {"U"}, "C6_BASE": {"F0", "F1", "U"},
        "W3_REAL_INTENTS_FIXED_REFERENCE_U": {"U"},
        "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE": set(),
        "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK": set(),
    }
    for intervention, enabled in expected.items():
        engine._c6_diagnostic_request = {"intervention_id": intervention}
        assert {item for item in ("F0", "F1", "U") if engine._c6_feature_enabled(item)} == enabled
'''
new_ablation = '''def test_ablation_map_is_exact_and_production_defaults_full_on() -> None:
    production = BacktestEngine()
    assert production._runtime_policy.state_local_books is True
    assert production._runtime_policy.block_retained_defensive_rebuy is True
    assert production._runtime_policy.use_fixed_reference_scores is True
    expected = {
        "BASELINE": set(), "F0_ONLY": {"F0"}, "F0_F1": {"F0", "F1"},
        "U_ONLY": {"U"}, "C6_BASE": {"F0", "F1", "U"},
        "W3_REAL_INTENTS_FIXED_REFERENCE_U": {"U"},
        "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE": set(),
        "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK": set(),
    }
    for intervention, enabled in expected.items():
        policy = runtime_policy_for_intervention(intervention)
        actual = {
            name
            for name, value in {
                "F0": policy.state_local_books,
                "F1": policy.block_retained_defensive_rebuy,
                "U": policy.use_fixed_reference_scores,
            }.items()
            if value
        }
        assert actual == enabled
'''
test_text = one(test_text, old_ablation, new_ablation, "diagnostic feature-map test")
test_text = test_text.replace(
    "from quantfusion.engine.ensemble_orchestration import capture_c6_warm_state",
    "from quantfusion.engine.ensemble_orchestration import capture_diagnostic_warm_state",
)
test_text = test_text.replace(
    "captured = capture_c6_warm_state(states, risk, None)",
    "captured = capture_diagnostic_warm_state(states, risk, None)",
)
test_text = test_text.replace(
    "capture_c6_warm_state(states, risk, None)",
    "capture_diagnostic_warm_state(states, risk, None)",
)
test_text = one(
    test_text,
    "    assert blocked['summary']['warmup_not_ready'] is True\n",
    "    assert blocked['warmup_health']['warmup_status'] == 'INVALID'\n    assert blocked['summary']['buys_suppressed'] is True\n",
    "readiness health assertion",
)
old_runner = '''        runner = engine.run
        if intervention != 'PRODUCTION':
            runner = engine.run_c6_diagnostic
            kwargs['diagnostic_request'] = {'schema_version': 1, 'intervention_id': intervention,
                                           'recording_mode': 'DEFAULT', 'scenario_id': 'synthetic-healthy-bull',
                                           'diagnostic_noncanonical': True, 'allow_publication': False}
        result = runner({symbol: symbol for symbol in symbols}, '2026-01-05', '2026-01-09', **kwargs)
'''
new_runner = '''        if intervention == 'PRODUCTION':
            result = engine.run(
                {symbol: symbol for symbol in symbols},
                '2026-01-05', '2026-01-09', **kwargs,
            )
        else:
            result = run_c6_diagnostic(
                engine,
                {symbol: symbol for symbol in symbols},
                '2026-01-05', '2026-01-09',
                diagnostic_request={
                    'schema_version': 1, 'intervention_id': intervention,
                    'recording_mode': 'DEFAULT', 'scenario_id': 'synthetic-healthy-bull',
                    'diagnostic_noncanonical': True, 'allow_publication': False,
                },
                **kwargs,
            )
'''
test_text = one(test_text, old_runner, new_runner, "healthy bull research runner")
save(str(p), test_text)

p, retained_text = load("tests/c6_non_economic/test_c6_retained_winner.py")
retained_text = retained_text.replace("_c6_exposure_trace", "_diagnostic_exposure_trace")
retained_text = retained_text.replace("_record_c6_exposure", "_record_diagnostic_exposure")
save(str(p), retained_text)

p, budget_text = load("tests/c6_non_economic/test_account_risk_budget.py")
if "from quantfusion.research.c6_runtime import run_c6_diagnostic\n" not in budget_text:
    anchor = "import pytest\n"
    budget_text = one(
        budget_text,
        anchor,
        anchor + "from quantfusion.research.c6_runtime import run_c6_diagnostic\n",
        "budget research adapter import",
    )
old_budget = '''    from quantfusion.engine.replay import ProductionReplayEngine
    engine = ProductionReplayEngine(cfg={'account_risk_budget_enabled': True})
    with pytest.raises(ValueError, match="own evidence identity"):
        engine.run_c6_diagnostic({}, '2025-04-01', '2026-07-20',
            data_dir='unused', regime_data_dir='unused', diagnostic_request={})
'''
new_budget = '''    from quantfusion.engine.replay import ProductionReplayEngine
    engine = ProductionReplayEngine(cfg={'account_risk_budget_enabled': True})
    with pytest.raises(ValueError, match="own evidence identity"):
        run_c6_diagnostic(
            engine, {}, '2025-04-01', '2026-07-20',
            data_dir='unused', regime_data_dir='unused', diagnostic_request={},
        )
'''
budget_text = one(budget_text, old_budget, new_budget, "budget adapter test")
save(str(p), budget_text)

"""
text = text.replace(marker, block + marker, 1)
p.write_text(text, encoding='utf-8')
