"""Lifecycle ownership for strategies created by the production route controller."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from quantfusion.strategy.trend import BaseStrategy


class StrategyLifecycleState(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    CLEANUP_REQUIRED = "cleanup_required"


@dataclass(slots=True)
class StrategyLifecycleEntry:
    sleeve_name: str
    symbol: str
    strategy: BaseStrategy
    state: StrategyLifecycleState = StrategyLifecycleState.INACTIVE


class StrategyLifecycleRegistry:
    """Own registration and retirement for controller-created strategies.

    Inactive strategies stay owned by the registry so their cooldown state can
    survive a later weak episode.  They are removed from a sleeve's external
    registry until a leader, live position, or pending order makes them active.
    Entries outside the current universe are retired only after they no longer
    own live or pending account state.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], StrategyLifecycleEntry] = {}
        self._cleanup_events: list[dict[str, str]] = []

    def get(self, sleeve_name: str, symbol: str) -> StrategyLifecycleEntry | None:
        return self._entries.get((sleeve_name, symbol))

    def acquire(
        self,
        sleeve_name: str,
        symbol: str,
        factory: Callable[[], BaseStrategy],
    ) -> StrategyLifecycleEntry:
        key = (sleeve_name, symbol)
        entry = self._entries.get(key)
        if entry is None:
            entry = StrategyLifecycleEntry(sleeve_name, symbol, factory())
            self._entries[key] = entry
        return entry

    @staticmethod
    def _registered(sleeve: Any, symbol: str, strategy: BaseStrategy) -> bool:
        return any(
            owner is strategy
            for owner in sleeve.external_strategy_instances.get(symbol, ())
        )

    @classmethod
    def _register(cls, sleeve: Any, entry: StrategyLifecycleEntry) -> None:
        registered = sleeve.external_strategy_instances.setdefault(entry.symbol, [])
        if not cls._registered(sleeve, entry.symbol, entry.strategy):
            registered.append(entry.strategy)

    @staticmethod
    def _unregister(sleeve: Any, entry: StrategyLifecycleEntry) -> None:
        registered = sleeve.external_strategy_instances.get(entry.symbol)
        if registered is None:
            return
        kept = [owner for owner in registered if owner is not entry.strategy]
        if kept:
            sleeve.external_strategy_instances[entry.symbol] = kept
        else:
            sleeve.external_strategy_instances.pop(entry.symbol, None)

    def activate(self, sleeve: Any, entry: StrategyLifecycleEntry) -> None:
        entry.state = StrategyLifecycleState.ACTIVE
        self._register(sleeve, entry)

    @staticmethod
    def _has_pending_owner(
        entry: StrategyLifecycleEntry, pending: Iterable[tuple[Any, BaseStrategy]]
    ) -> bool:
        return any(owner is entry.strategy for _, owner in pending)

    def reconcile(
        self,
        sleeve: Any,
        *,
        current_symbols: Iterable[str],
        active_symbols: Iterable[str],
        pending: Iterable[tuple[Any, BaseStrategy]],
        date: str,
    ) -> None:
        """Align registration with account ownership after each close."""
        sleeve_name = str(sleeve.sleeve_name)
        current = set(current_symbols)
        active = set(active_symbols)
        pending_items = tuple(pending)
        retire: list[tuple[str, str]] = []
        for key, entry in tuple(self._entries.items()):
            if entry.sleeve_name != sleeve_name:
                continue
            owns_state = entry.strategy.position is not None or self._has_pending_owner(
                entry, pending_items
            )
            if entry.symbol in active or owns_state:
                self.activate(sleeve, entry)
                continue
            self._unregister(sleeve, entry)
            if entry.symbol in current:
                entry.state = StrategyLifecycleState.INACTIVE
                continue
            entry.state = StrategyLifecycleState.CLEANUP_REQUIRED
            retire.append(key)

        for key in retire:
            entry = self._entries.pop(key)
            self._cleanup_events.append(
                {
                    "date": date,
                    "sleeve": entry.sleeve_name,
                    "symbol": entry.symbol,
                    "event": "weak_strategy_cleanup",
                }
            )

    def entries(self) -> tuple[StrategyLifecycleEntry, ...]:
        return tuple(
            self._entries[key]
            for key in sorted(self._entries)
        )

    def snapshot(self) -> dict[str, list[dict[str, str]]]:
        return {
            "entries": [
                {
                    "sleeve": entry.sleeve_name,
                    "symbol": entry.symbol,
                    "state": entry.state.value,
                }
                for entry in self.entries()
            ],
            "cleanup_events": list(self._cleanup_events),
        }


__all__ = [
    "StrategyLifecycleEntry",
    "StrategyLifecycleRegistry",
    "StrategyLifecycleState",
]
