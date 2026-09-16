"""Research adapter that maps frozen C6 identities onto the generic engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.engine.runtime import ReplayRuntimePolicy


_INTERVENTIONS = frozenset(
    {
        "BASELINE",
        "F0_ONLY",
        "F0_F1",
        "U_ONLY",
        "C6_BASE",
        "C6_BASE_PLUS_S",
        "C6_BASE_AB5",
        "C6_BASE_AB5_PLUS_S",
        "W0_NO_601869",
        "W1_DATA_MAP_ONLY",
        "W2_POOL_DENOMINATOR_ONLY",
        "W3_REAL_INTENTS_FIXED_REFERENCE_U",
        "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE",
        "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK",
    }
)
_AB5_INTERVENTIONS = frozenset({"C6_BASE_AB5", "C6_BASE_AB5_PLUS_S"})
_FEATURES = {
    "BASELINE": frozenset(),
    "F0_ONLY": frozenset({"F0"}),
    "F0_F1": frozenset({"F0", "F1"}),
    "U_ONLY": frozenset({"U"}),
    "C6_BASE": frozenset({"F0", "F1", "U"}),
    "C6_BASE_PLUS_S": frozenset({"F0", "F1", "U", "S"}),
    "C6_BASE_AB5": frozenset({"F0", "F1", "U"}),
    "C6_BASE_AB5_PLUS_S": frozenset({"F0", "F1", "U", "S"}),
    "W0_NO_601869": frozenset(),
    "W1_DATA_MAP_ONLY": frozenset(),
    "W2_POOL_DENOMINATOR_ONLY": frozenset(),
    "W3_REAL_INTENTS_FIXED_REFERENCE_U": frozenset({"U"}),
    "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE": frozenset(),
    "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK": frozenset(),
}
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "intervention_id",
        "recording_mode",
        "scenario_id",
        "diagnostic_noncanonical",
        "allow_publication",
    }
)


@dataclass(frozen=True, slots=True)
class C6DiagnosticRequest:
    """Validated frozen research identity, owned outside the production engine."""

    schema_version: int
    intervention_id: str
    recording_mode: str
    scenario_id: str
    diagnostic_noncanonical: bool
    allow_publication: bool

    @classmethod
    def from_mapping(cls, request: Mapping[str, Any]) -> "C6DiagnosticRequest":
        if not isinstance(request, Mapping) or any(
            not isinstance(key, str) for key in request
        ):
            raise ValueError("diagnostic_request must be an object with string keys")
        missing = sorted(_REQUEST_KEYS - set(request))
        extra = sorted(set(request) - _REQUEST_KEYS)
        if missing or extra:
            raise ValueError(f"diagnostic_request has missing={missing} extra={extra}")
        if request["schema_version"] != 1 or isinstance(request["schema_version"], bool):
            raise ValueError("schema_version must be literal integer 1")
        intervention = request["intervention_id"]
        if intervention not in _INTERVENTIONS:
            raise ValueError("intervention_id is not in the frozen enum")
        recording_mode = request["recording_mode"]
        if recording_mode not in {"DEFAULT", "OFF", "ON"}:
            raise ValueError("recording_mode is not in the frozen enum")
        scenario = request["scenario_id"]
        if not isinstance(scenario, str) or not scenario:
            raise ValueError("scenario_id must be a nonempty string")
        if recording_mode != "DEFAULT":
            no_drift = scenario in {"add-one-13-601869", "random-20260807-03-004"} or (
                scenario.startswith("prefix-")
                and scenario[7:] in {f"{n:02d}" for n in range(5, 18)}
            )
            if intervention not in {
                "C6_BASE",
                "C6_BASE_PLUS_S",
                "C6_BASE_AB5",
                "C6_BASE_AB5_PLUS_S",
            } or not no_drift:
                raise ValueError(
                    "recording_mode is restricted to the frozen selected-candidate no-drift manifest"
                )
        if request["diagnostic_noncanonical"] is not True:
            raise ValueError("diagnostic_noncanonical must be literal true")
        if request["allow_publication"] is not False:
            raise ValueError("allow_publication must be literal false")
        return cls(
            schema_version=1,
            intervention_id=str(intervention),
            recording_mode=str(recording_mode),
            scenario_id=scenario,
            diagnostic_noncanonical=True,
            allow_publication=False,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intervention_id": self.intervention_id,
            "recording_mode": self.recording_mode,
            "scenario_id": self.scenario_id,
            "diagnostic_noncanonical": self.diagnostic_noncanonical,
            "allow_publication": self.allow_publication,
        }

    def runtime_policy(self) -> ReplayRuntimePolicy:
        return runtime_policy_for_intervention(
            self.intervention_id,
            recording=self.recording_mode != "OFF",
        )


def runtime_policy_for_intervention(
    intervention_id: str, *, recording: bool = True
) -> ReplayRuntimePolicy:
    """Map one frozen research identity onto generic engine capabilities."""
    if intervention_id not in _INTERVENTIONS:
        raise ValueError("intervention_id is not in the frozen enum")
    features = _FEATURES[intervention_id]
    signal_exclusions = (
        frozenset({"601869"})
        if intervention_id in {"W1_DATA_MAP_ONLY", "W2_POOL_DENOMINATOR_ONLY"}
        else frozenset()
    )
    allocation_exclusions = (
        frozenset({"601869"})
        if intervention_id == "W1_DATA_MAP_ONLY"
        else frozenset()
    )
    return ReplayRuntimePolicy(
        diagnostics_enabled=True,
        recording_enabled=recording,
        compare_score_families=recording and intervention_id.startswith("W"),
        state_local_books="F0" in features,
        block_retained_defensive_rebuy="F1" in features,
        use_fixed_reference_scores="U" in features,
        overlay_s_enabled="S" in features,
        merged_portfolio_lock_enabled=(
            intervention_id != "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK"
        ),
        allocation_data_exclusions=allocation_exclusions,
        signal_universe_exclusions=signal_exclusions,
    )


def validate_c6_diagnostic_request(request: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the frozen C6 request without coupling it to replay classes."""
    return C6DiagnosticRequest.from_mapping(request).as_dict()


def c6_diagnostic_engine_config(
    cfg: Mapping[str, Any], request: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind the account budget only to its explicit historical AB5 identities."""
    out = dict(cfg)
    intervention = str(request.get("intervention_id", ""))
    enabled = intervention in _AB5_INTERVENTIONS
    if out.get("account_risk_budget_enabled", False) and not enabled:
        raise ValueError("AB5 requires its own evidence identity, not a frozen C6 Base/S run")
    out["account_risk_budget_enabled"] = enabled
    return out


def run_c6_diagnostic(
    engine: ProductionReplayEngine,
    symbols_dict: dict[str, str],
    start_date: str,
    end_date: str,
    *,
    data_dir: str,
    regime_data_dir: str,
    leader_data_dir: str | None = None,
    diagnostic_request: Mapping[str, Any],
    **run_kwargs: Any,
) -> dict[str, Any]:
    """Run one frozen research intervention through the canonical replay path."""
    request = C6DiagnosticRequest.from_mapping(diagnostic_request)
    cfg = c6_diagnostic_engine_config(engine.cfg, request.as_dict())
    diagnostic_symbols = dict(symbols_dict)
    if request.intervention_id == "W0_NO_601869":
        diagnostic_symbols.pop("601869", None)
    delegate = ProductionReplayEngine(
        engine.initial_capital,
        cfg=cfg,
        policy=engine.policy,
    )
    result = delegate.run(
        diagnostic_symbols,
        start_date,
        end_date,
        data_dir=data_dir,
        regime_data_dir=regime_data_dir,
        leader_data_dir=leader_data_dir,
        runtime_policy=request.runtime_policy(),
        **run_kwargs,
    )
    engine.delegate = delegate.delegate
    diagnostics = result.pop("_runtime_diagnostics", {})
    legacy_names = {
        "sleeve_results": "_c6_sleeve_results",
        "states": "_c6_states",
        "warm_state": "_c6_warm_state",
        "score_trace": "_c6_score_trace",
        "orders": "_c6_orders",
        "fills": "_c6_fills",
        "pending_path": "_c6_pending_path",
        "exposure_trace": "_c6_exposure_trace",
    }
    for generic_name, research_name in legacy_names.items():
        result[research_name] = diagnostics.get(
            generic_name, [] if generic_name != "warm_state" else {}
        )
    result["diagnostic_request"] = request.as_dict()
    result["requested_symbols"] = sorted(symbols_dict)
    result["selected_symbols"] = sorted(diagnostic_symbols)
    result["deployment_policy"] = "production_daily_replay"
    return result


__all__ = [
    "C6DiagnosticRequest",
    "c6_diagnostic_engine_config",
    "run_c6_diagnostic",
    "runtime_policy_for_intervention",
    "validate_c6_diagnostic_request",
]
