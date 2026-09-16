"""Shared market-input health semantics for production and research paths."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class DataHealthStatus(str, Enum):
    """Outcome of validating decision-critical input evidence."""

    VALID = "valid"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class DataHealthIssue:
    """One unavailable or invalid input observed at a component boundary."""

    source: str
    status: DataHealthStatus
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "status": self.status.value,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class DataHealthReport:
    """Structured health for inputs without changing domain decisions."""

    issues: tuple[DataHealthIssue, ...] = ()

    @property
    def status(self) -> DataHealthStatus:
        if any(issue.status is DataHealthStatus.INVALID for issue in self.issues):
            return DataHealthStatus.INVALID
        if self.issues:
            return DataHealthStatus.UNAVAILABLE
        return DataHealthStatus.VALID

    @property
    def valid(self) -> bool:
        return self.status is DataHealthStatus.VALID

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "issues": [issue.as_dict() for issue in self.issues],
        }

    @classmethod
    def from_issues(cls, issues: Iterable[DataHealthIssue]) -> "DataHealthReport":
        return cls(tuple(issues))


def issue_from_exception(source: str, error: BaseException) -> DataHealthIssue:
    """Classify expected input failures at the I/O/domain boundary."""
    invalid = isinstance(error, (KeyError, TypeError, ValueError))
    status = DataHealthStatus.INVALID if invalid else DataHealthStatus.UNAVAILABLE
    return DataHealthIssue(source=source, status=status, message=str(error))


__all__ = [
    "DataHealthIssue",
    "DataHealthReport",
    "DataHealthStatus",
    "issue_from_exception",
]
