"""Causal metadata contract for decision-time alpha features."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import pandas as pd

from quantfusion.domain.health import HealthState


@dataclass(frozen=True, slots=True)
class CausalFeatureContract:
    """Declare and validate the time boundary of one alpha feature."""

    feature_name: str
    as_of_date: str
    required_history: int
    source_columns: tuple[str, ...]
    validation_status: HealthState

    def as_dict(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "as_of_date": self.as_of_date,
            "required_history": self.required_history,
            "source_columns": list(self.source_columns),
            "validation_status": self.validation_status.value,
        }


def validate_causal_feature_frame(
    *,
    feature_name: str,
    as_of_date: str | pd.Timestamp,
    required_history: int,
    source_columns: Sequence[str],
    frame: pd.DataFrame,
) -> CausalFeatureContract:
    """Return a causal feature declaration with its current validation state.

    Definition errors are programmer errors and raise immediately. Data-quality
    problems are represented in ``validation_status`` so callers can fail closed
    or deliberately fall back without inventing a second health vocabulary.
    """

    name = str(feature_name).strip()
    if not name:
        raise ValueError("feature_name must be non-empty")
    if required_history < 1:
        raise ValueError("required_history must be positive")

    columns = tuple(str(column).strip() for column in source_columns)
    if not columns or any(not column for column in columns):
        raise ValueError("source_columns must be non-empty")
    if len(columns) != len(set(columns)):
        raise ValueError("source_columns must not contain duplicates")

    boundary = pd.Timestamp(as_of_date)
    if boundary is pd.NaT:
        raise ValueError("as_of_date must not be NaT")
    boundary = boundary.normalize()

    status = HealthState.READY
    if any(column not in frame.columns for column in columns):
        status = HealthState.INVALID
    else:
        try:
            index = pd.DatetimeIndex(pd.to_datetime(frame.index, errors="coerce"))
            normalized_index = index.normalize()
            invalid_index = (
                normalized_index.isna().any()
                or normalized_index.has_duplicates
                or not normalized_index.is_monotonic_increasing
            )
            has_future = bool((normalized_index > boundary).any())
        except (TypeError, ValueError):
            invalid_index = True
            has_future = False

        if invalid_index or has_future:
            status = HealthState.INVALID
        elif len(frame) < required_history:
            status = HealthState.DEGRADED

    return CausalFeatureContract(
        feature_name=name,
        as_of_date=str(boundary.date()),
        required_history=required_history,
        source_columns=columns,
        validation_status=status,
    )


__all__ = ["CausalFeatureContract", "validate_causal_feature_frame"]
