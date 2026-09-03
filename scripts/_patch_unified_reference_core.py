#!/usr/bin/env python3
"""Temporary branch-only separation of tradable cores and signal references."""

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one occurrence, found {count}")
    return text.replace(old, new, 1)


universe_path = Path("quantfusion/config/universe.py")
universe = universe_path.read_text(encoding="utf-8")
universe = replace_once(
    universe,
    "ESTABLISHED_EXPANSION_CORE = frozenset(ORDERED_SYMBOLS[:13])",
    "ESTABLISHED_BASE_CORE = frozenset(ORDERED_SYMBOLS[:5])\n"
    "ESTABLISHED_EXPANSION_CORE = frozenset(ORDERED_SYMBOLS[:13])",
    "tradable core constants",
)
universe_path.write_text(universe, encoding="utf-8")

allocation_path = Path("quantfusion/engine/ensemble_allocation.py")
allocation = allocation_path.read_text(encoding="utf-8")
allocation = replace_once(
    allocation,
    "from quantfusion.config.universe import ESTABLISHED_EXPANSION_CORE",
    "from quantfusion.config.universe import (\n"
    "    ESTABLISHED_BASE_CORE,\n"
    "    ESTABLISHED_EXPANSION_CORE,\n"
    ")",
    "allocation universe imports",
)
allocation = replace_once(
    allocation,
    "_ESTABLISHED_EXPANSION_CORE = ESTABLISHED_EXPANSION_CORE",
    "_ESTABLISHED_BASE_CORE = ESTABLISHED_BASE_CORE\n"
    "_ESTABLISHED_EXPANSION_CORE = ESTABLISHED_EXPANSION_CORE",
    "allocation universe aliases",
)
allocation = replace_once(
    allocation,
    "        reference_core = set(self.policy.regime_symbols)",
    "        reference_core = set(_ESTABLISHED_BASE_CORE)",
    "five-symbol tradable core",
)
allocation_path.write_text(allocation, encoding="utf-8")

orchestration_path = Path("quantfusion/engine/ensemble_orchestration.py")
orchestration = orchestration_path.read_text(encoding="utf-8")
class_anchor = '''class EnsembleOrchestrationMixin:
    """Cross-market evidence loading and synchronized ensemble replay."""

'''
method = '''class EnsembleOrchestrationMixin:
    """Cross-market evidence loading and synchronized ensemble replay."""

    @staticmethod
    def _reference_evidence_complete(
        states: list[_PreparedSleeveRun], regime_symbols: tuple[str, ...]
    ) -> bool:
        """Require every sleeve to load the fixed signal-only reference basket."""
        expected = set(regime_symbols)
        return bool(states) and all(
            expected.issubset(state.data_map) for state in states
        )

'''
orchestration = replace_once(
    orchestration,
    class_anchor,
    method,
    "reference evidence helper",
)
old_reference = '''        reference_complete = set(self.policy.regime_symbols).issubset(
            request.symbols_dict
        )
        # The incomplete-reference reserve protects the trend candidate scorer
        # when its fixed comparison basket is unavailable. A replay that starts
        # defensively instead delegates candidate selection to the weak-leader
        # controller, so applying that trend-only reserve would duplicate risk
        # controls and prematurely lock the routed account. Bull-start replay
        # and ordinary trend runs keep the reserve and account thresholds.
        starts_defensive = bool(
            getattr(request.route_controller, "starts_defensive", False)
        )
        self._runtime_reference_complete = (
            reference_complete or starts_defensive
        )'''
new_reference = '''        # MarketRegimeMixin loads the fixed signal-only reference basket beside
        # the tradable pool and fails closed on missing data. Tradable membership
        # therefore must not be used as a proxy for reference-data completeness.
        self._runtime_reference_complete = True'''
orchestration = replace_once(
    orchestration,
    old_reference,
    new_reference,
    "runtime reference semantics",
)
old_states = "        states = self._prepare_ensemble_sleeves(request, effective_policy)\n"
new_states = '''        states = self._prepare_ensemble_sleeves(request, effective_policy)
        if not self._reference_evidence_complete(
            states, self.policy.regime_symbols
        ):
            raise RuntimeError(
                "fixed signal-only regime reference evidence is incomplete"
            )
'''
orchestration = replace_once(
    orchestration,
    old_states,
    new_states,
    "post-load reference validation",
)
orchestration_path.write_text(orchestration, encoding="utf-8")
