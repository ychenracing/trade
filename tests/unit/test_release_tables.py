"""Compact review tables must retain every measured value and scenario identity."""

import json

import pytest

from quantfusion.application import native_joint


def test_tables_round_trip_and_detect_tampering(tmp_path):
    from scripts.release_tables import read_tables, write_tables

    source = native_joint.load_incumbent_reference()
    write_tables(source, tmp_path)
    assert read_tables(tmp_path) == source
    table = next(tmp_path.glob("random_subset-*.json"))
    body = json.loads(table.read_text())
    body["rows"][0][1] += 1
    table.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="hash"):
        read_tables(tmp_path)


def test_tables_reject_changed_scenario_definition(tmp_path):
    from scripts.release_tables import write_tables

    source = native_joint.load_incumbent_reference()
    source["results"][0]["symbols"] = ["invalid"]
    with pytest.raises(ValueError, match="scenario"):
        write_tables(source, tmp_path)
