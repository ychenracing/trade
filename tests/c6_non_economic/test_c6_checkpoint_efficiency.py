from __future__ import annotations

import pytest

from quantfusion.application.c6_bound_run import DiagnosticCheckpoint


def _square(value: int) -> dict[str, int]:
    return {"square": value * value}


def test_checkpoint_serializes_only_when_handing_off(tmp_path, monkeypatch) -> None:
    ids = [f"scenario/{index}" for index in range(3)]
    path = tmp_path / "child-checkpoint.json"
    checkpoint = DiagnosticCheckpoint(
        path,
        ids,
        "a" * 64,
        budget_seconds=60,
        chunk_size=1,
    )
    calls = 0
    original = checkpoint.save

    def counted_save() -> None:
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(checkpoint, "save", counted_save)
    actual = checkpoint.map(_square, [0, 1, 2], ids, workers=1)

    assert list(actual) == [_square(index) for index in range(3)]
    assert calls == 0
    assert not path.exists()


def test_checkpoint_serializes_once_before_graceful_exit(tmp_path, monkeypatch) -> None:
    ids = [f"scenario/{index}" for index in range(3)]
    path = tmp_path / "child-checkpoint.json"
    checkpoint = DiagnosticCheckpoint(
        path,
        ids,
        "a" * 64,
        budget_seconds=0,
        chunk_size=1,
    )
    calls = 0
    original = checkpoint.save

    def counted_save() -> None:
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(checkpoint, "save", counted_save)
    with pytest.raises(SystemExit) as stopped:
        checkpoint.map(_square, [0, 1, 2], ids, workers=1)

    assert stopped.value.code == 75
    assert calls == 1
    assert path.is_file()
