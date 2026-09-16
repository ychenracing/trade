from pathlib import Path


def update(path: str, replacements: list[tuple[str, str]]) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"missing expected health migration anchor in {path}: {old!r}")
        text = text.replace(old, new)
    p.write_text(text, encoding="utf-8")


update(
    "quantfusion/engine/replay.py",
    [
        ('selection.status != "valid"', 'selection.status != "READY"'),
        (".require_valid(", ".require_ready("),
    ],
)
update(
    "tests/unit/test_data_health_and_strategy_lifecycle.py",
    [('asdict(result)["health"]["status"]', 'asdict(result)["health"]["state"]')],
)
update(
    "tests/unit/test_production_reliability_boundaries.py",
    [
        ('== "valid"', '== "READY"'),
        ('== "unavailable"', '== "DEGRADED"'),
        ('== "invalid"', '== "INVALID"'),
    ],
)
update(
    "tests/unit/test_daily_report.py",
    [
        (
            'data["summary"].update(buys_suppressed=True, )\n',
            'data["summary"].update(buys_suppressed=True)\n'
            '        data["warmup_health"]["warmup_status"] = "INVALID"\n',
        )
    ],
)
