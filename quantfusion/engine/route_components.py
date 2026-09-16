"""Focused helpers for production-route orchestration."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from quantfusion.regime.evidence import select_positive_momentum_leaders
from quantfusion.regime.models import LeaderSelection
from quantfusion.strategy.weak import PositiveMomentumHoldStrategy


LeaderSelectionFunction = Callable[..., LeaderSelection]


class LeaderSelector:
    """Own point-in-time leader selection, caching, and input-health reporting."""

    def __init__(
        self,
        leader_data_dir: str | Path,
        *,
        event_sink: list[dict[str, Any]],
        selector: LeaderSelectionFunction | None = None,
    ) -> None:
        self._leader_data_dir = str(leader_data_dir)
        self._event_sink = event_sink
        self._selector = selector or select_positive_momentum_leaders
        self._cache: dict[str, LeaderSelection] = {}

    def select(self, symbols: Sequence[str], date_str: str) -> tuple[str, ...]:
        selection = self._cache.get(date_str)
        if selection is None:
            selection = self._selector(
                tuple(symbols),
                data_dir=self._leader_data_dir,
                as_of=date_str,
            )
            self._cache[date_str] = selection
        if selection.status != "READY":
            self._event_sink.append(
                {
                    "date": date_str,
                    "event": "leader_selection_failure",
                    "status": selection.status,
                    "unavailable_symbols": list(selection.unavailable_symbols),
                    "invalid_symbols": list(selection.invalid_symbols),
                    "health": selection.health.as_dict(),
                }
            )
        selection.require_ready("production route")
        return tuple(selection.selected_symbols)


class CashAllocator:
    """Move idle sleeve cash without changing account-level assets or risk basis."""

    @staticmethod
    def shift_free_cash(states: list[Any], weights: Sequence[float]) -> None:
        total_cash = sum(float(state.sleeve.cash) for state in states)
        if total_cash <= 0:
            return
        targets = [total_cash * float(weight) for weight in weights]
        targets[-1] = total_cash - sum(targets[:-1])
        for state, target in zip(states, targets, strict=True):
            old = float(state.sleeve.cash)
            if not state.sleeve.equity_curve:
                raise RuntimeError("route cash migration requires a closing equity sample")
            closing = state.sleeve.equity_curve[-1]
            assets_before = float(closing["assets"])
            state.sleeve.cash = target
            flow = target - old
            state.sleeve.risk.rebase_after_cash_flow(assets_before, flow)
            closing["assets"] = assets_before + flow
            closing["cash"] = float(closing["cash"]) + flow


class ExecutionGuard:
    """Own route-level pending-order suppression and liquidation queue rules."""

    @staticmethod
    def drop_buys(states: list[Any]) -> None:
        for state in states:
            state.pending = [
                item for item in state.pending if item[0].direction != "buy"
            ]

    @staticmethod
    def queue_liquidations(
        states: list[Any], date_str: str, *, weak_only: bool
    ) -> None:
        for state in states:
            liquidations = state.sleeve._generate_liquidation_signals(
                date_str,
                reason="production outer-route migration",
            )
            selected = [
                item
                for item in liquidations
                if (
                    item[0].strategy_name == PositiveMomentumHoldStrategy.name
                ) == weak_only
            ]
            if not selected:
                continue
            state.pending = state.sleeve._dedupe_pending_signals(
                [item for item in state.pending if item[0].direction == "sell"]
                + selected
            )


__all__ = ["CashAllocator", "ExecutionGuard", "LeaderSelector"]
