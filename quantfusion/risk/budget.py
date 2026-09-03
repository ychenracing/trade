"""Pure ex-ante drawdown-budget arithmetic and hysteretic state."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskBook:
    """One strategy book valued with information known at the close."""

    symbol: str
    group: str
    shares: int
    mark_price: float
    entry_price: float
    stop_price: float
    atr: float


@dataclass(frozen=True, slots=True)
class PortfolioAdverseLoss:
    """Transparent group-stressed adverse-loss estimate."""

    projected_loss: float
    risk_driver_loss: float
    group_losses: dict[str, float]
    complete: bool
    missing_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DrawdownBudgetSnapshot:
    """One close-known account budget observation."""

    current_assets: float
    lifetime_peak_assets: float
    drawdown: float
    drawdown_floor: float
    remaining_cushion: float
    execution_buffer: float
    available_budget: float
    projected_loss: float
    risk_driver_loss: float
    group_losses: dict[str, float]
    projected_loss_ratio: float
    evidence_complete: bool
    missing_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DrawdownBudgetDecision:
    """State and limits to apply to the next executable session."""

    state: str
    allow_new_risk: bool
    new_risk_capacity: float
    reduction_fraction: float
    cushion_worsened: bool
    risk_driver_worsened: bool


def _finite_positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def portfolio_adverse_loss(
    books: list[RiskBook] | tuple[RiskBook, ...],
    *,
    adverse_atr_multiple: float = 1.0,
    other_group_loss_weight: float = 0.5,
) -> PortfolioAdverseLoss:
    """Charge full same-group loss and a fixed share of other-group loss."""
    if not _finite_positive(adverse_atr_multiple):
        raise ValueError("adverse_atr_multiple must be positive and finite")
    if (
        isinstance(other_group_loss_weight, bool)
        or not isinstance(other_group_loss_weight, (int, float))
        or not math.isfinite(float(other_group_loss_weight))
        or not 0 <= float(other_group_loss_weight) <= 1
    ):
        raise ValueError("other_group_loss_weight must be in [0, 1]")

    group_losses: dict[str, float] = {}
    group_driver_losses: dict[str, float] = {}
    missing: set[str] = set()
    for book in books:
        if book.shares <= 0:
            continue
        values_complete = all(
            _finite_positive(value)
            for value in (
                book.mark_price,
                book.entry_price,
                book.stop_price,
                book.atr,
            )
        )
        if values_complete:
            stressed_atr = float(adverse_atr_multiple) * float(book.atr)
            driver_unit_loss = max(
                float(book.entry_price) - float(book.stop_price),
                stressed_atr,
                0.0,
            )
            marked_unit_loss = driver_unit_loss
        else:
            missing.add(str(book.symbol))
            marked_unit_loss = (
                float(book.mark_price) if _finite_positive(book.mark_price) else 0.0
            )
            driver_unit_loss = marked_unit_loss
        group = str(book.group or "unmapped")
        group_losses[group] = group_losses.get(group, 0.0) + (
            int(book.shares) * marked_unit_loss
        )
        group_driver_losses[group] = group_driver_losses.get(group, 0.0) + (
            int(book.shares) * driver_unit_loss
        )

    def aggregate(losses: dict[str, float]) -> float:
        if not losses:
            return 0.0
        largest = max(losses.values())
        return largest + float(other_group_loss_weight) * (sum(losses.values()) - largest)

    return PortfolioAdverseLoss(
        projected_loss=aggregate(group_losses),
        risk_driver_loss=aggregate(group_driver_losses),
        group_losses=dict(sorted(group_losses.items())),
        complete=not missing,
        missing_symbols=tuple(sorted(missing)),
    )


class DrawdownBudgetController:
    """Apply a lifetime-peak budget with explicit underwater hysteresis."""

    def __init__(
        self,
        *,
        max_drawdown: float = 0.18,
        base_budget_peak_fraction: float = 0.175,
        execution_buffer_peak_fraction: float = 0.005,
        adverse_atr_multiple: float = 1.0,
        other_group_loss_weight: float = 0.5,
        constraint_release_ratio: float = 0.8,
        minimum_reentry_cooldown: int = 5,
        recovery_confirmation_days: int = 3,
        minimum_drawdown_recovery: float = 0.02,
        normal_state_drawdown_exit: float = 0.05,
    ) -> None:
        ratios = {
            "max_drawdown": max_drawdown,
            "base_budget_peak_fraction": base_budget_peak_fraction,
            "execution_buffer_peak_fraction": execution_buffer_peak_fraction,
            "constraint_release_ratio": constraint_release_ratio,
            "minimum_drawdown_recovery": minimum_drawdown_recovery,
            "normal_state_drawdown_exit": normal_state_drawdown_exit,
        }
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 1
            for value in ratios.values()
        ):
            raise ValueError("drawdown budget ratios must be finite values in [0, 1]")
        if base_budget_peak_fraction > max_drawdown:
            raise ValueError("base budget must not exceed the drawdown limit")
        if execution_buffer_peak_fraction >= max_drawdown:
            raise ValueError("execution buffer must leave positive drawdown cushion")
        if minimum_reentry_cooldown < 1 or recovery_confirmation_days < 1:
            raise ValueError("hysteresis day counts must be positive integers")
        self.max_drawdown = float(max_drawdown)
        self.base_budget_peak_fraction = float(base_budget_peak_fraction)
        self.execution_buffer_peak_fraction = float(
            execution_buffer_peak_fraction
        )
        self.adverse_atr_multiple = float(adverse_atr_multiple)
        self.other_group_loss_weight = float(other_group_loss_weight)
        self.constraint_release_ratio = float(constraint_release_ratio)
        self.minimum_reentry_cooldown = int(minimum_reentry_cooldown)
        self.recovery_confirmation_days = int(recovery_confirmation_days)
        self.minimum_drawdown_recovery = float(minimum_drawdown_recovery)
        self.normal_state_drawdown_exit = float(normal_state_drawdown_exit)
        self.state = "normal"
        self.entered_position: int | None = None
        self.max_constrained_drawdown = 0.0
        self.recovery_streak = 0
        self._previous_cushion: float | None = None
        self._previous_risk_driver_loss: float | None = None
        self._last_reduction_ratio: float | None = None

    def snapshot(
        self,
        current_assets: float,
        lifetime_peak_assets: float,
        books: list[RiskBook] | tuple[RiskBook, ...],
    ) -> DrawdownBudgetSnapshot:
        """Compute the current budget without mutating lifetime-peak ownership."""
        assets = max(float(current_assets), 0.0)
        peak = max(float(lifetime_peak_assets), assets, 0.0)
        floor = peak * (1.0 - self.max_drawdown)
        cushion = max(assets - floor, 0.0)
        buffer = peak * self.execution_buffer_peak_fraction
        budget = max(
            min(peak * self.base_budget_peak_fraction, cushion - buffer),
            0.0,
        )
        estimate = portfolio_adverse_loss(
            books,
            adverse_atr_multiple=self.adverse_atr_multiple,
            other_group_loss_weight=self.other_group_loss_weight,
        )
        ratio = (
            estimate.projected_loss / budget
            if budget > 0
            else (math.inf if estimate.projected_loss > 0 else 0.0)
        )
        drawdown = (peak - assets) / peak if peak > 0 else 0.0
        return DrawdownBudgetSnapshot(
            current_assets=assets,
            lifetime_peak_assets=peak,
            drawdown=drawdown,
            drawdown_floor=floor,
            remaining_cushion=cushion,
            execution_buffer=buffer,
            available_budget=budget,
            projected_loss=estimate.projected_loss,
            risk_driver_loss=estimate.risk_driver_loss,
            group_losses=estimate.group_losses,
            projected_loss_ratio=ratio,
            evidence_complete=estimate.complete,
            missing_symbols=estimate.missing_symbols,
        )

    def decide(
        self,
        snapshot: DrawdownBudgetSnapshot,
        *,
        position: int,
        warning_active: bool,
        has_pending_reduction: bool = False,
    ) -> DrawdownBudgetDecision:
        """Advance the deterministic state and return next-session limits."""
        cushion_worsened = (
            self._previous_cushion is not None
            and snapshot.remaining_cushion < self._previous_cushion - 1e-12
        )
        risk_driver_worsened = (
            self._previous_risk_driver_loss is not None
            and snapshot.risk_driver_loss
            > self._previous_risk_driver_loss + 1e-12
        )
        over_budget = snapshot.projected_loss_ratio > 1.0 + 1e-12
        just_entered = False

        if self.state in {"normal", "recovering"} and (
            over_budget or warning_active
        ):
            self.state = "constrained"
            self.entered_position = int(position)
            self.max_constrained_drawdown = snapshot.drawdown
            self.recovery_streak = 0
            self._last_reduction_ratio = None
            just_entered = True

        if self.state == "constrained":
            self.max_constrained_drawdown = max(
                self.max_constrained_drawdown, snapshot.drawdown
            )
            elapsed = (
                int(position) - self.entered_position
                if self.entered_position is not None
                else 0
            )
            recovered_drawdown = (
                self.max_constrained_drawdown - snapshot.drawdown
                >= self.minimum_drawdown_recovery - 1e-12
                or snapshot.projected_loss <= 1e-12
            )
            release_ready = (
                not warning_active
                and not over_budget
                and snapshot.projected_loss_ratio
                <= self.constraint_release_ratio + 1e-12
                and elapsed >= self.minimum_reentry_cooldown
                and recovered_drawdown
            )
            self.recovery_streak = self.recovery_streak + 1 if release_ready else 0
            if self.recovery_streak >= self.recovery_confirmation_days:
                self.state = "recovering"
                self.recovery_streak = 0

        elif self.state == "recovering":
            normal_ready = (
                not warning_active
                and snapshot.projected_loss_ratio
                <= self.constraint_release_ratio + 1e-12
                and snapshot.drawdown <= self.normal_state_drawdown_exit + 1e-12
            )
            self.recovery_streak = self.recovery_streak + 1 if normal_ready else 0
            if self.recovery_streak >= self.recovery_confirmation_days:
                self.state = "normal"
                self.entered_position = None
                self.max_constrained_drawdown = 0.0
                self.recovery_streak = 0

        reduction_fraction = 0.0
        materially_worse = (
            self._last_reduction_ratio is not None
            and snapshot.projected_loss_ratio
            > self._last_reduction_ratio / self.constraint_release_ratio + 1e-12
        )
        reduction_driver = (
            just_entered
            or materially_worse
        ) and (
            self._previous_cushion is None
            or cushion_worsened
            or risk_driver_worsened
        )
        if (
            over_budget
            and reduction_driver
            and not has_pending_reduction
            and snapshot.projected_loss > 0
        ):
            reduction_fraction = max(
                0.0,
                min(
                    1.0,
                    1.0 - snapshot.available_budget / snapshot.projected_loss,
                ),
            )
            self._last_reduction_ratio = snapshot.projected_loss_ratio

        allow_new = self.state in {"normal", "recovering"} and not warning_active
        capacity = (
            max(snapshot.available_budget - snapshot.projected_loss, 0.0)
            if allow_new
            else 0.0
        )
        self._previous_cushion = snapshot.remaining_cushion
        self._previous_risk_driver_loss = snapshot.risk_driver_loss
        return DrawdownBudgetDecision(
            state=self.state,
            allow_new_risk=allow_new,
            new_risk_capacity=capacity,
            reduction_fraction=reduction_fraction,
            cushion_worsened=cushion_worsened,
            risk_driver_worsened=risk_driver_worsened,
        )


__all__ = [
    "DrawdownBudgetController",
    "DrawdownBudgetDecision",
    "DrawdownBudgetSnapshot",
    "PortfolioAdverseLoss",
    "RiskBook",
    "portfolio_adverse_loss",
]
