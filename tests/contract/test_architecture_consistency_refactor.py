"""Architecture contracts for production/research and health-state ownership."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.engine.universe import BacktestEngine
from quantfusion.risk.governance import assess_warmup_health


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "quantfusion"


def test_health_state_is_domain_owned() -> None:
    """Health semantics belong to the domain, not to one data adapter."""
    assert (PACKAGE / "domain" / "health.py").is_file()
    assert not (PACKAGE / "data" / "health.py").exists()


def test_warmup_uses_unified_invalid_state_for_unusable_inputs() -> None:
    report = assess_warmup_health(
        {},
        "2026-01-05",
        "2026-01-30",
        regime_index_frames={},
    )

    assert report.warmup_status == "INVALID"


def test_allocation_degradation_uses_shared_health_report() -> None:
    def fail_scores(data_map, date):
        del data_map, date
        raise ValueError("invalid allocation evidence")

    sleeve = SimpleNamespace(
        sleeve_name="fast",
        positions={"held": {"dual_ma": SimpleNamespace(shares=100)}},
        _allocation_scores=fail_scores,
        risk_events=[],
    )
    state = SimpleNamespace(sleeve=sleeve, data_map={"held": object()})

    score = BacktestEngine._overlay_allocation_score(
        [state], pd.Timestamp("2026-01-05")
    )

    assert score("held") == 0.0
    assert score.health.state.value == "DEGRADED"
    assert score.health.issues[0].code == "invalid_calculation"


def test_production_replay_has_no_research_diagnostic_entrypoint() -> None:
    """Research diagnostics must adapt the production replay API externally."""
    assert not hasattr(ProductionReplayEngine, "run_c6_diagnostic")
    assert not hasattr(ProductionReplayEngine, "validate_c6_diagnostic_request")


def test_c6_intervention_identities_do_not_branch_inside_engine() -> None:
    """Production engine behavior is controlled by generic runtime policy."""
    forbidden = {
        "W0_NO_601869",
        "W1_DATA_MAP_ONLY",
        "W2_POOL_DENOMINATOR_ONLY",
        "W3_REAL_INTENTS_FIXED_REFERENCE_U",
        "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE",
        "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK",
        "_c6_intervention_id",
        "_c6_feature_enabled",
    }
    violations: list[str] = []
    for path in (PACKAGE / "engine").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                violations.append(f"{path.relative_to(ROOT)}: {token}")

    assert violations == []


def test_research_owns_c6_runtime_adapter() -> None:
    assert (PACKAGE / "research" / "c6_runtime.py").is_file()
