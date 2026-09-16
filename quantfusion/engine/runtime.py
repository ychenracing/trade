"""Generic runtime policy for production replay and external research adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ReplayRuntimePolicy:
    """Runtime behavior switches expressed without research experiment identities.

    The production default is the canonical fully enabled engine. Research
    adapters may construct alternate policies for controlled ablations without
    teaching the engine about experiment names or evidence identities.
    """

    diagnostics_enabled: bool = False
    recording_enabled: bool = False
    compare_score_families: bool = False
    state_local_books: bool = True
    block_retained_defensive_rebuy: bool = True
    use_fixed_reference_scores: bool = True
    overlay_s_enabled: bool = False
    merged_portfolio_lock_enabled: bool = True
    allocation_data_exclusions: frozenset[str] = frozenset()
    signal_universe_exclusions: frozenset[str] = frozenset()

    @property
    def production(self) -> bool:
        return not self.diagnostics_enabled


def runtime_policy(owner: Any) -> ReplayRuntimePolicy:
    """Return an owner's explicit policy or the canonical production policy."""
    policy = getattr(owner, "_runtime_policy", None)
    return policy if isinstance(policy, ReplayRuntimePolicy) else ReplayRuntimePolicy()


__all__ = ["ReplayRuntimePolicy", "runtime_policy"]
