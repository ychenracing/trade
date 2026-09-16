"""Shared health-state model for decision-critical system boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class HealthState(str, Enum):
    """Operational usability of an evaluated component."""

    READY = "READY"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


_SEVERITY = {
    HealthState.READY: 0,
    HealthState.DEGRADED: 1,
    HealthState.INVALID: 2,
}


@dataclass(frozen=True, slots=True)
class HealthIssue:
    """One explicit reason why a component is not fully ready."""

    source: str
    state: HealthState
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "state": self.state.value,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Aggregate health without conflating valid emptiness with bad inputs."""

    issues: tuple[HealthIssue, ...] = ()
    state: HealthState = field(init=False)

    def __post_init__(self) -> None:
        state = HealthState.READY
        for issue in self.issues:
            if _SEVERITY[issue.state] > _SEVERITY[state]:
                state = issue.state
        object.__setattr__(self, "state", state)

    @property
    def ready(self) -> bool:
        return self.state is HealthState.READY

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "issues": [issue.as_dict() for issue in self.issues],
        }

    @classmethod
    def from_issues(cls, issues: Iterable[HealthIssue]) -> "HealthReport":
        return cls(tuple(issues))


def unavailable_issue(source: str, message: str) -> HealthIssue:
    """Represent an incomplete input that remains inspectable but degraded."""
    return HealthIssue(source, HealthState.DEGRADED, "unavailable_input", message)


def invalid_issue(source: str, message: str) -> HealthIssue:
    """Represent input that cannot be treated as valid evidence."""
    return HealthIssue(source, HealthState.INVALID, "invalid_input", message)


def invalid_calculation_issue(source: str, message: str) -> HealthIssue:
    """Represent a failed calculation whose caller has an explicit safe fallback."""
    return HealthIssue(source, HealthState.DEGRADED, "invalid_calculation", message)


def issue_from_exception(source: str, error: BaseException) -> HealthIssue:
    """Classify expected I/O and validation failures at a component boundary."""
    if isinstance(error, (KeyError, TypeError, ValueError)):
        return invalid_issue(source, str(error))
    return unavailable_issue(source, str(error))


__all__ = [
    "HealthIssue",
    "HealthReport",
    "HealthState",
    "invalid_calculation_issue",
    "invalid_issue",
    "issue_from_exception",
    "unavailable_issue",
]
