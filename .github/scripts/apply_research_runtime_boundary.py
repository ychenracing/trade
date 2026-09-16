from __future__ import annotations

from pathlib import Path
import re


def load(path: str) -> tuple[Path, str]:
    p = Path(path)
    return p, p.read_text(encoding="utf-8")


def one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


def save(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


# Generic ensemble runtime contract.
p, text = load("quantfusion/engine/ensemble.py")
text = one(
    text,
    "from quantfusion.engine.causal import CausalBacktestEngine\n",
    "from quantfusion.engine.causal import CausalBacktestEngine\nfrom quantfusion.engine.runtime import ReplayRuntimePolicy\n",
    "ensemble runtime import",
)
text = one(
    text,
    "    warmup_calendar_days: int\n    risk_state: dict | None = None\n",
    "    warmup_calendar_days: int\n    runtime_policy: ReplayRuntimePolicy = field(default_factory=ReplayRuntimePolicy)\n    risk_state: dict | None = None\n",
    "run request runtime",
)
text = one(
    text,
    "        self.sleeves: list[_EnsembleSleeveBacktestEngine] = []\n        self.last_result: dict | None = None\n",
    "        self.sleeves: list[_EnsembleSleeveBacktestEngine] = []\n        self.last_result: dict | None = None\n        self._runtime_policy = ReplayRuntimePolicy()\n",
    "engine runtime state",
)
text = one(
    text,
    "        route_controller: Any | None = None,\n    ) -> dict:\n",
    "        route_controller: Any | None = None,\n        runtime_policy: ReplayRuntimePolicy | None = None,\n    ) -> dict:\n",
    "run runtime arg",
)
text = one(
    text,
    "        mode = str(allocation_mode or self.policy.allocation_mode).lower()\n",
    "        effective_runtime = runtime_policy or ReplayRuntimePolicy()\n        self._runtime_policy = effective_runtime\n        mode = str(allocation_mode or self.policy.allocation_mode).lower()\n",
    "effective runtime",
)
text = one(
    text,
    "                warmup_calendar_days=warmup_calendar_days,\n                risk_state=risk_state,\n",
    "                warmup_calendar_days=warmup_calendar_days,\n                runtime_policy=effective_runtime,\n                risk_state=risk_state,\n",
    "run request construction",
)
save(str(p), text)


# Allocation consumes capabilities, never experiment identities.
p, text = load("quantfusion/engine/ensemble_allocation.py")
text = one(
    text,
    "from quantfusion.engine.ensemble import PreparedSleeveRun, RunRequest\n",
    "from quantfusion.engine.ensemble import PreparedSleeveRun, RunRequest\nfrom quantfusion.engine.runtime import runtime_policy\n",
    "allocation runtime import",
)
start = text.index("    def _c6_feature_enabled(self, feature: str) -> bool:\n")
end = text.index("    def _assess_run_warmup_health(\n", start)
text = text[:start] + text[end:]
text = text.replace("_record_c6_exposure", "_record_diagnostic_exposure")
text = text.replace("_c6_exposure_trace", "_diagnostic_exposure_trace")
text = one(
    text,
    "            diagnostic = getattr(self, \"_c6_diagnostic_request\", None)\n            sleeve._c6_intervention = self._c6_intervention_id()\n            if diagnostic is not None and diagnostic[\"recording_mode\"] != \"OFF\":\n",
    "            runtime = runtime_policy(self)\n            sleeve._runtime_policy = runtime\n            if runtime.recording_enabled:\n",
    "sleeve diagnostic activation",
)
text = one(
    text,
    "        score_receipts = []\n        for state_index, state in enumerate(states):\n",
    "        score_receipts = []\n        runtime = runtime_policy(self)\n        for state_index, state in enumerate(states):\n",
    "authorize runtime",
)
text = one(
    text,
    "            score_data_map = state.data_map\n            if self._c6_intervention_id() == \"W1_DATA_MAP_ONLY\":\n                score_data_map = {\n                    symbol: frame\n                    for symbol, frame in state.data_map.items()\n                    if symbol != \"601869\"\n                }\n",
    "            score_data_map = {\n                symbol: frame\n                for symbol, frame in state.data_map.items()\n                if symbol not in runtime.allocation_data_exclusions\n            }\n",
    "allocation exclusions",
)
text = text.replace('self._c6_feature_enabled("U")', "runtime.use_fixed_reference_scores")
text = text.replace('getattr(self, "_c6_score_trace", None)', 'getattr(self, "_diagnostic_score_trace", None)')
text = text.replace('self._c6_score_trace.append(receipt)', 'self._diagnostic_score_trace.append(receipt)')
text = one(
    text,
    '                if (self._c6_intervention_id() or "").startswith("W"):\n',
    "                if runtime.compare_score_families:\n",
    "score family comparison",
)
text = one(
    text,
    "        carried_symbols = self._held_portfolio_symbols(states)\n        self._record_diagnostic_exposure(states, date, \"batch_start\")\n",
    "        carried_symbols = self._held_portfolio_symbols(states)\n        runtime = runtime_policy(self)\n        self._record_diagnostic_exposure(states, date, \"batch_start\")\n",
    "open runtime",
)
text = text.replace('if self._c6_feature_enabled("F1"):', "if runtime.block_retained_defensive_rebuy:")
text = text.replace("carried_symbols if runtime.use_fixed_reference_scores else None", "carried_symbols if runtime.use_fixed_reference_scores else None")
text = text.replace('setattr(state.sleeve, "_c6_buy_scores", admission_scores if runtime.use_fixed_reference_scores else None)', 'setattr(state.sleeve, "_runtime_buy_scores", admission_scores if runtime.use_fixed_reference_scores else None)')
text = text.replace('delattr(state.sleeve, "_c6_buy_scores")', 'delattr(state.sleeve, "_runtime_buy_scores")')
save(str(p), text)


# Low-level engines consume only generic exclusion capabilities.
p, text = load("quantfusion/engine/data_flow.py")
text = one(
    text,
    "from quantfusion.indicators.technical import Indicators\n",
    "from quantfusion.indicators.technical import Indicators\nfrom quantfusion.engine.runtime import runtime_policy\n",
    "data flow runtime import",
)
text = one(
    text,
    '        if getattr(self, "_c6_intervention", None) in {"W1_DATA_MAP_ONLY", "W2_POOL_DENOMINATOR_ONLY"}:\n            data_map = {code: frame for code, frame in data_map.items() if code != "601869"}\n',
    "        exclusions = runtime_policy(self).signal_universe_exclusions\n        if exclusions:\n            data_map = {code: frame for code, frame in data_map.items() if code not in exclusions}\n",
    "data flow exclusions",
)
save(str(p), text)

p, text = load("quantfusion/engine/causal.py")
text = one(
    text,
    "from quantfusion.engine.core import CoreBacktestEngine\n",
    "from quantfusion.engine.core import CoreBacktestEngine\nfrom quantfusion.engine.runtime import runtime_policy\n",
    "causal runtime import",
)
text = one(
    text,
    '        if getattr(self, "_c6_intervention", None) == "W1_DATA_MAP_ONLY":\n            data_map = {code: frame for code, frame in data_map.items() if code != "601869"}\n',
    "        exclusions = runtime_policy(self).allocation_data_exclusions\n        if exclusions:\n            data_map = {code: frame for code, frame in data_map.items() if code not in exclusions}\n",
    "causal exclusions",
)
text = text.replace('getattr(self, "_c6_buy_scores", None)', 'getattr(self, "_runtime_buy_scores", None)')
save(str(p), text)

p, text = load("quantfusion/engine/replay_loop.py")
text = one(
    text,
    "from quantfusion.domain.rules import SYMBOL_RE\n",
    "from quantfusion.domain.rules import SYMBOL_RE\nfrom quantfusion.engine.runtime import runtime_policy\n",
    "replay loop runtime import",
)
text = one(
    text,
    '        if getattr(self, "_c6_intervention", None) in {"W1_DATA_MAP_ONLY", "W2_POOL_DENOMINATOR_ONLY"}:\n            symbols_dict = {code: name for code, name in symbols_dict.items() if code != "601869"}\n',
    "        exclusions = runtime_policy(self).signal_universe_exclusions\n        if exclusions:\n            symbols_dict = {code: name for code, name in symbols_dict.items() if code not in exclusions}\n",
    "replay loop exclusions",
)
save(str(p), text)


# Ensemble orchestration records generic diagnostics behind the runtime policy.
p, text = load("quantfusion/engine/ensemble_orchestration.py")
text = one(
    text,
    "from quantfusion.engine.ensemble import PreparedSleeveRun, RunRequest\n",
    "from quantfusion.engine.ensemble import PreparedSleeveRun, RunRequest\nfrom quantfusion.engine.runtime import runtime_policy\n",
    "orchestration runtime import",
)
text = text.replace("capture_c6_warm_state", "capture_diagnostic_warm_state")
text = one(
    text,
    '        diagnostic = getattr(self, "_c6_diagnostic_request", None)\n        self._c6_score_trace = [] if diagnostic is not None and diagnostic["recording_mode"] != "OFF" else None\n        self._c6_exposure_trace = [] if diagnostic is not None and diagnostic["recording_mode"] != "OFF" else None\n',
    "        runtime = request.runtime_policy\n        self._runtime_policy = runtime\n        self._diagnostic_score_trace = [] if runtime.recording_enabled else None\n        self._diagnostic_exposure_trace = [] if runtime.recording_enabled else None\n",
    "orchestration diagnostic init",
)
text = one(
    text,
    '''            setattr(
                cm_overlay,
                "_c6_s_enabled",
                self._c6_feature_enabled("S")
                if diagnostic is not None
                else bool(getattr(cm_overlay, "C6_S_PRODUCTION", False)),
            )
            setattr(
                cm_overlay,
                "_c6_diagnostic_evidence_enabled",
                diagnostic is not None and diagnostic["recording_mode"] != "OFF",
            )
''',
    '''            setattr(cm_overlay, "_c6_s_enabled", runtime.overlay_s_enabled)
            setattr(
                cm_overlay,
                "_c6_diagnostic_evidence_enabled",
                runtime.recording_enabled,
            )
''',
    "overlay runtime flags",
)
text = text.replace(" if diagnostic is not None else None", " if runtime.diagnostics_enabled else None")
text = text.replace("if diagnostic is not None:\n                pending_path.append", "if runtime.diagnostics_enabled:\n                pending_path.append")
text = one(
    text,
    '''                if self._c6_intervention_id() in {
                    "W1_DATA_MAP_ONLY",
                    "W2_POOL_DENOMINATOR_ONLY",
                }:
                    state.pending = [
                        item for item in state.pending if item[0].symbol != "601869"
                    ]
''',
    '''                if runtime.signal_universe_exclusions:
                    state.pending = [
                        item
                        for item in state.pending
                        if item[0].symbol not in runtime.signal_universe_exclusions
                    ]
''',
    "pending exclusions",
)
text = one(
    text,
    '''                route_symbols = request.symbols_dict
                if self._c6_intervention_id() in {"W1_DATA_MAP_ONLY", "W2_POOL_DENOMINATOR_ONLY"}:
                    route_symbols = {code: name for code, name in route_symbols.items() if code != "601869"}
''',
    '''                route_symbols = {
                    code: name
                    for code, name in request.symbols_dict.items()
                    if code not in runtime.signal_universe_exclusions
                }
''',
    "route exclusions",
)
text = one(
    text,
    '''            if (
                self._c6_intervention_id()
                != "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK"
            ):
''',
    "            if runtime.merged_portfolio_lock_enabled:\n",
    "merged lock runtime",
)
text = text.replace('state_local_books=self._c6_feature_enabled("F0")', "state_local_books=runtime.state_local_books")
text = text.replace('if diagnostic is not None and diagnostic["recording_mode"] != "OFF":', "if runtime.recording_enabled:")
text = text.replace("self._record_c6_exposure", "self._record_diagnostic_exposure")
old = '''        request_data = getattr(self, "_c6_diagnostic_request", None)
        if request_data is not None:
            combined["_c6_sleeve_results"] = results
            combined["_c6_states"] = states
            combined["_c6_warm_state"] = warm_state
            combined["_c6_score_trace"] = self._c6_score_trace
            combined["_c6_orders"] = getattr(states[0].sleeve, "_c6_orders", [])
            combined["_c6_fills"] = getattr(states[0].sleeve, "_c6_fills", [])
            combined["_c6_pending_path"] = pending_path
            combined["_c6_exposure_trace"] = self._c6_exposure_trace
'''
new = '''        if runtime.diagnostics_enabled:
            combined["_runtime_diagnostics"] = {
                "sleeve_results": results,
                "states": states,
                "warm_state": warm_state,
                "score_trace": self._diagnostic_score_trace,
                "orders": getattr(states[0].sleeve, "_c6_orders", []),
                "fills": getattr(states[0].sleeve, "_c6_fills", []),
                "pending_path": pending_path,
                "exposure_trace": self._diagnostic_exposure_trace,
            }
'''
text = one(text, old, new, "generic diagnostic output")
save(str(p), text)


# Production replay exposes only production runtime; frozen C6 identity lives in research.
p, text = load("quantfusion/engine/replay.py")
text = one(
    text,
    "from collections.abc import Callable, Mapping, Sequence\n",
    "from collections.abc import Callable, Sequence\n",
    "replay mapping import",
)
text = one(
    text,
    "from quantfusion.engine.universe import BacktestEngine, SleeveBacktestEngine\n",
    "from quantfusion.engine.runtime import ReplayRuntimePolicy\nfrom quantfusion.engine.universe import BacktestEngine, SleeveBacktestEngine\n",
    "replay runtime import",
)
start = text.index("_C6_DIAGNOSTIC_REQUEST_KEYS = {")
end = text.index("class ProductionRouteController:", start)
text = text[:start] + text[end:]
start = text.index("    @staticmethod\n    def validate_c6_diagnostic_request(", text.index("class ProductionReplayEngine:"))
end = text.index("    def run(\n", start)
text = text[:start] + text[end:]
text = one(
    text,
    "        risk_state: dict | None = None,\n    ) -> dict[str, Any]:\n",
    "        risk_state: dict | None = None,\n        runtime_policy: ReplayRuntimePolicy | None = None,\n    ) -> dict[str, Any]:\n",
    "production runtime arg",
)
text = one(
    text,
    "            risk_state=risk_state,\n            route_controller=controller,\n",
    "            risk_state=risk_state,\n            route_controller=controller,\n            runtime_policy=runtime_policy,\n",
    "production runtime forwarding",
)
start = text.index("    def run_c6_diagnostic(\n", text.index("class ProductionReplayEngine:"))
end = text.index("\n\nclass RegimeAdaptiveBacktestEngine:", start)
text = text[:start] + text[end:]
save(str(p), text)


# Research application calls its adapter explicitly instead of a production method.
p, text = load("quantfusion/application/c6_diagnostics.py")
insert = "from quantfusion.research.c6_runtime import run_c6_diagnostic\n"
anchor = "from quantfusion.application.c6_predicates import (\n"
text = one(text, anchor, insert + anchor, "c6 diagnostics research import")
pattern = re.compile(r"ProductionReplayEngine\(([^\n]+)\)\.run_c6_diagnostic\(\n")
text, count = pattern.subn(r"run_c6_diagnostic(\n            ProductionReplayEngine(\1),\n", text)
if count != 2:
    raise SystemExit(f"expected two application diagnostic callsites, got {count}")
save(str(p), text)


# Research helper centralizes identity-to-capability mapping for tests and tooling.
p, text = load("quantfusion/research/c6_runtime.py")
old = '''    def runtime_policy(self) -> ReplayRuntimePolicy:
        features = _FEATURES[self.intervention_id]
        signal_exclusions = (
            frozenset({"601869"})
            if self.intervention_id in {"W1_DATA_MAP_ONLY", "W2_POOL_DENOMINATOR_ONLY"}
            else frozenset()
        )
        allocation_exclusions = (
            frozenset({"601869"})
            if self.intervention_id == "W1_DATA_MAP_ONLY"
            else frozenset()
        )
        recording = self.recording_mode != "OFF"
        return ReplayRuntimePolicy(
            diagnostics_enabled=True,
            recording_enabled=recording,
            compare_score_families=recording and self.intervention_id.startswith("W"),
            state_local_books="F0" in features,
            block_retained_defensive_rebuy="F1" in features,
            use_fixed_reference_scores="U" in features,
            overlay_s_enabled="S" in features,
            merged_portfolio_lock_enabled=(
                self.intervention_id != "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK"
            ),
            allocation_data_exclusions=allocation_exclusions,
            signal_universe_exclusions=signal_exclusions,
        )
'''
new = '''    def runtime_policy(self) -> ReplayRuntimePolicy:
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
'''
text = one(text, old, new, "research runtime helper")
text = text.replace('result["deployment_policy"] = "diagnostic_noncanonical"', 'result["requested_symbols"] = sorted(symbols_dict)\n    result["selected_symbols"] = sorted(diagnostic_symbols)\n    result["deployment_policy"] = "production_daily_replay"')
text = text.replace('    "run_c6_diagnostic",\n', '    "run_c6_diagnostic",\n    "runtime_policy_for_intervention",\n')
save(str(p), text)


# Migrate tests to research-owned identities/capabilities, not engine internals.
p, text = load("tests/c6_non_economic/test_c6_formal_ab5.py")
text = one(
    text,
    "from quantfusion.engine.replay import (\n    ProductionReplayEngine,\n    c6_diagnostic_engine_config,\n)\n",
    "from quantfusion.engine.replay import ProductionReplayEngine\nfrom quantfusion.research.c6_runtime import (\n    c6_diagnostic_engine_config,\n    runtime_policy_for_intervention,\n    validate_c6_diagnostic_request,\n)\n",
    "formal research imports",
)
text = text.replace("ProductionReplayEngine.validate_c6_diagnostic_request(request)", "validate_c6_diagnostic_request(request)")
old = '''    engine = object.__new__(BacktestEngine)
    for intervention, expected in (
        ("C6_BASE_AB5", {"F0", "F1", "U"}),
        ("C6_BASE_AB5_PLUS_S", {"F0", "F1", "U", "S"}),
    ):
        engine._c6_diagnostic_request = {"intervention_id": intervention}
        assert {
            feature
            for feature in ("F0", "F1", "U", "S")
            if engine._c6_feature_enabled(feature)
        } == expected
'''
new = '''    del BacktestEngine
    for intervention, expected in (
        ("C6_BASE_AB5", {"F0", "F1", "U"}),
        ("C6_BASE_AB5_PLUS_S", {"F0", "F1", "U", "S"}),
    ):
        policy = runtime_policy_for_intervention(intervention)
        enabled = {
            name
            for name, value in {
                "F0": policy.state_local_books,
                "F1": policy.block_retained_defensive_rebuy,
                "U": policy.use_fixed_reference_scores,
                "S": policy.overlay_s_enabled,
            }.items()
            if value
        }
        assert enabled == expected
'''
text = one(text, old, new, "formal feature mapping test")
save(str(p), text)

p, text = load("tests/unit/test_default_account_budget.py")
text = one(
    text,
    "from quantfusion.engine.replay import c6_diagnostic_engine_config\n",
    "from quantfusion.research.c6_runtime import c6_diagnostic_engine_config\n",
    "default budget research import",
)
save(str(p), text)

# Broad mechanical test migration for remaining research-only internals.
for name in [
    "tests/c6_non_economic/test_c6_diagnostics.py",
    "tests/c6_non_economic/test_c6_retained_winner.py",
    "tests/c6_non_economic/test_c6_fixed_reference_admission.py",
]:
    p, text = load(name)
    if "runtime_policy_for_intervention" not in text:
        anchor = "import pytest\n" if "import pytest\n" in text else "from unittest import mock\n"
        if anchor not in text:
            anchor = "from unittest.mock import "
            pos = text.find(anchor)
            if pos < 0:
                # place after future import
                anchor = "from __future__ import annotations\n"
                text = one(text, anchor, anchor + "\nfrom quantfusion.research.c6_runtime import runtime_policy_for_intervention\n", f"{name} runtime import")
            else:
                line_end = text.find("\n", pos)
                text = text[:line_end+1] + "from quantfusion.research.c6_runtime import runtime_policy_for_intervention\n" + text[line_end+1:]
        else:
            text = one(text, anchor, anchor + "from quantfusion.research.c6_runtime import runtime_policy_for_intervention\n", f"{name} runtime import")
    text = re.sub(
        r'(\w+)\._c6_diagnostic_request = \{"intervention_id": (\w+)\}',
        r'\1._runtime_policy = runtime_policy_for_intervention(\2)',
        text,
    )
    text = re.sub(
        r'(\w+)\._c6_diagnostic_request = \{"intervention_id": "([A-Z0-9_]+)"\}',
        r'\1._runtime_policy = runtime_policy_for_intervention("\2")',
        text,
    )
    save(str(p), text)

# Architecture contract should not see research identities in production engine files.
for path in Path("quantfusion/engine").glob("*.py"):
    source = path.read_text(encoding="utf-8")
    for forbidden in (
        "W0_NO_601869",
        "W1_DATA_MAP_ONLY",
        "W2_POOL_DENOMINATOR_ONLY",
        "W3_REAL_INTENTS_FIXED_REFERENCE_U",
        "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE",
        "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK",
        "_c6_intervention_id",
        "_c6_feature_enabled",
    ):
        if forbidden in source:
            raise SystemExit(f"research identity remains in {path}: {forbidden}")
