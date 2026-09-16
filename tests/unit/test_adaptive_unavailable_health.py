from unittest.mock import patch

from quantfusion.engine import replay as replay_module
from quantfusion.engine.replay import RegimeAdaptiveBacktestEngine, SleeveBacktestEngine
from quantfusion.regime.models import DeploymentDecision, RegimeEvidence


def test_all_unavailable_trend_pool_reports_degraded_health() -> None:
    engine = RegimeAdaptiveBacktestEngine()
    decision = DeploymentDecision(
        name="frozen_trend_engine",
        boundary="2026-01-30",
        reason="test",
        regime=RegimeEvidence(
            as_of="2026-01-30",
            regime="trending",
            observations=(),
        ),
        leaders=None,
    )

    with (
        patch.object(engine, "decide", return_value=decision),
        patch.object(engine, "_available_local_symbols", return_value={}),
        patch.object(SleeveBacktestEngine, "run", return_value={}),
        patch.object(replay_module, "simulate_route_sequence", return_value=[]),
    ):
        result = engine.run(
            {"300308": "example"},
            "2026-02-02",
            "2026-02-06",
            data_dir="unused",
            regime_data_dir="unused",
            deployment_mode="trend",
            allow_unavailable_symbols=True,
        )

    assert result["deployment_decision"]["name"] == "cash_preservation"
    assert result["deployment_decision"]["leaders"]["health"]["state"] == "DEGRADED"
    assert result["unavailable_symbols"] == ["300308"]
