"""Strict v3 account-snapshot truth boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quantfusion.account import snapshot as snapshot_module


def _valid_payload() -> dict[str, object]:
    return {
        "schema_version": 3,
        "account_id": "main",
        "snapshot_date": "2026-02-01",
        "cash": 100_000.0,
        "peak_equity": 1_000_000.0,
        "positions": {
            "300308": {
                "shares": 100,
                "sellable_shares": 40,
                "avg_cost": 100.0,
                "entry_date": "2026-01-02",
                "highest_close": 120.0,
            }
        },
    }


def _write_payload(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "account.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=True),
        encoding="utf-8",
    )
    return path


def test_valid_v3_snapshot_preserves_required_truth(tmp_path: Path) -> None:
    snapshot = snapshot_module.load_account_snapshot(
        _write_payload(tmp_path, _valid_payload())
    )

    assert snapshot.schema_version == 3
    assert snapshot.account_id == "main"
    assert snapshot.snapshot_date == "2026-02-01"
    assert snapshot.cash == 100_000.0
    assert snapshot.peak_equity == 1_000_000.0
    assert len(snapshot.positions) == 1
    position = snapshot.positions[0]
    assert position.symbol == "300308"
    assert position.shares == 100
    assert position.sellable_shares == 40
    assert position.avg_cost == 100.0
    assert position.entry_date == "2026-01-02"
    assert position.highest_close == 120.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", None),
        ("schema_version", 2),
        ("schema_version", 4),
        ("schema_version", True),
        ("account_id", None),
        ("account_id", ""),
        ("account_id", True),
        ("snapshot_date", None),
        ("snapshot_date", "2026/02/01"),
        ("snapshot_date", "2026-2-1"),
        ("cash", None),
        ("cash", -1),
        ("cash", True),
        ("cash", float("nan")),
        ("peak_equity", None),
        ("peak_equity", 0),
        ("peak_equity", True),
        ("peak_equity", float("inf")),
        ("positions", None),
        ("positions", []),
        ("positions", "not-an-object"),
    ],
)
def test_required_root_fields_are_strict(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _valid_payload()
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value

    with pytest.raises(ValueError):
        snapshot_module.load_account_snapshot(_write_payload(tmp_path, payload))


@pytest.mark.parametrize(
    "unknown_field",
    [
        "cooldowns",
        "route_state",
        "risk_state",
        "pending_orders",
        "last_execution_report",
        "equity_history",
        "unexpected",
    ],
)
def test_unknown_root_fields_are_rejected(
    tmp_path: Path,
    unknown_field: str,
) -> None:
    payload = _valid_payload()
    payload[unknown_field] = {}

    with pytest.raises(ValueError, match="unknown root field"):
        snapshot_module.load_account_snapshot(_write_payload(tmp_path, payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shares", None),
        ("shares", 0),
        ("shares", 100.0),
        ("shares", True),
        ("sellable_shares", None),
        ("sellable_shares", -1),
        ("sellable_shares", 100.0),
        ("sellable_shares", True),
        ("sellable_shares", 101),
        ("avg_cost", None),
        ("avg_cost", 0),
        ("avg_cost", "100"),
        ("avg_cost", True),
        ("avg_cost", float("nan")),
        ("entry_date", None),
        ("entry_date", ""),
        ("entry_date", "2026/01/02"),
        ("entry_date", "2026-2-1"),
        ("entry_date", "2026-02-02"),
        ("entry_date", True),
        ("highest_close", 0),
        ("highest_close", True),
        ("highest_close", float("inf")),
    ],
)
def test_position_fields_are_strict(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _valid_payload()
    positions = payload["positions"]
    assert isinstance(positions, dict)
    position = positions["300308"]
    assert isinstance(position, dict)
    if value is None:
        position.pop(field)
    else:
        position[field] = value

    with pytest.raises(ValueError):
        snapshot_module.load_account_snapshot(_write_payload(tmp_path, payload))


@pytest.mark.parametrize(
    "unknown_field",
    ["position_source", "last_add_date", "price", "unexpected"],
)
def test_unknown_position_fields_are_rejected(
    tmp_path: Path,
    unknown_field: str,
) -> None:
    payload = _valid_payload()
    positions = payload["positions"]
    assert isinstance(positions, dict)
    position = positions["300308"]
    assert isinstance(position, dict)
    position[unknown_field] = "unsupported"

    with pytest.raises(ValueError, match="unknown position field"):
        snapshot_module.load_account_snapshot(_write_payload(tmp_path, payload))


def test_position_container_must_be_an_object(tmp_path: Path) -> None:
    payload = _valid_payload()
    positions = payload["positions"]
    assert isinstance(positions, dict)
    positions["300308"] = []

    with pytest.raises(ValueError, match="position 300308 must be an object"):
        snapshot_module.load_account_snapshot(_write_payload(tmp_path, payload))


def test_explicit_null_highest_close_is_rejected(tmp_path: Path) -> None:
    payload = _valid_payload()
    positions = payload["positions"]
    assert isinstance(positions, dict)
    position = positions["300308"]
    assert isinstance(position, dict)
    position["highest_close"] = None

    with pytest.raises(ValueError, match="highest_close must be a real number"):
        snapshot_module.load_account_snapshot(_write_payload(tmp_path, payload))


def test_nonstandard_json_constants_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "account.json"
    path.write_text(
        '{"schema_version":3,"account_id":"main",'
        '"snapshot_date":"2026-02-01","cash":NaN,'
        '"peak_equity":1000,"positions":{}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid JSON constant"):
        snapshot_module.load_account_snapshot(path)


def test_snapshot_sha256_covers_the_exact_input_bytes(tmp_path: Path) -> None:
    path = _write_payload(tmp_path, _valid_payload())
    expected = hashlib.sha256(path.read_bytes()).hexdigest()

    snapshot, digest = snapshot_module.load_account_snapshot_with_sha256(path)

    assert snapshot.account_id == "main"
    assert digest == expected
