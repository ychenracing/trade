# Current 17-symbol baseline and formal-stress documentation contracts.

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UNIVERSES = (
    ("1_symbol", "1"),
    ("3_symbols", "3"),
    ("5_symbols", "5"),
    ("13_symbols", "13"),
    ("17_symbols", "17"),
)


def _warm_result(name: str) -> dict[str, object]:
    payload = json.loads(
        (ROOT / "artifacts/validation/universe_backtest.json").read_text(
            encoding="utf-8"
        )
    )
    matches = [
        item
        for item in payload["results"]
        if item.get("universe") == name
        and item.get("indicator_state") == "warm"
        and item.get("end_date") == "2026-07-20"
    ]
    assert len(matches) == 1
    return matches[0]


def test_current_baseline_tables_match_the_frozen_artifact() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    validation = (ROOT / "docs/VALIDATION.md").read_text(encoding="utf-8")
    for name, label in UNIVERSES:
        item = _warm_result(name)
        row = (
            f"| {label} | {float(item['total_return']):.4%} | "
            f"{float(item['max_drawdown']):.4%} | "
            f"{int(item['total_trades'])} | "
            f"{int(item['date_symbol_side_count'])} |"
        )
        assert row in readme
        assert row in validation


def test_current_plan_wording_is_not_mixed_with_historical_983_evidence() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    validation = (ROOT / "docs/VALIDATION.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/ARCHITECTURE.md").read_text(
        encoding="utf-8"
    )
    data_readme = (ROOT / "data/README.md").read_text(encoding="utf-8")
    combined_current = "\n".join((readme, validation, architecture))
    assert "1/3/5/13/22" not in combined_current
    assert "共 983 次生产逐日回放" not in combined_current
    assert "全部 983 个正式场景" not in combined_current
    assert "精确 canonical 983" not in validation
    assert "983 场景计划能够进入正式发布校验" not in validation
    assert "add-one-05-688072" in readme
    assert "当前 17 只交易股票" in data_readme
    assert "历史 22 股完整 983 场景" in validation


def test_final_result_block_replaces_pending_text_after_publication() -> None:
    summary = (
        ROOT
        / "artifacts/validation/formal_stress_958_acceptance_summary.json"
    )
    if not summary.exists():
        return
    for relative in (
        "README.md",
        "docs/VALIDATION.md",
        "docs/ARCHITECTURE.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert (
            "完整 958 场景尚未在本次任务中形成最终工件" not in text
        )
        assert "完整计划已运行：`958/958`" in text
