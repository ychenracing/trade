from pathlib import Path

path = Path("quantfusion/engine/replay.py")
text = path.read_text(encoding="utf-8")

old_import = "from quantfusion.domain.models import BarContext\n"
new_import = (
    "from quantfusion.domain.health import HealthReport, unavailable_issue\n"
    "from quantfusion.domain.models import BarContext\n"
)
if text.count(old_import) != 1:
    raise SystemExit("domain import anchor changed")
text = text.replace(old_import, new_import, 1)

old = '''                leaders = LeaderSelection(
                    as_of=decision.boundary,
                    requested_symbols=tuple(sorted(symbols_dict)),
                    observed_symbols=0,
                    selected_symbols=(),
                    selected_returns=(),
                    unavailable_symbols=tuple(sorted(symbols_dict)),
                )
'''
new = '''                leaders = LeaderSelection(
                    as_of=decision.boundary,
                    requested_symbols=tuple(sorted(symbols_dict)),
                    observed_symbols=0,
                    selected_symbols=(),
                    selected_returns=(),
                    unavailable_symbols=tuple(sorted(symbols_dict)),
                    health=HealthReport.from_issues(
                        [
                            unavailable_issue(
                                "trend_symbols",
                                "no requested symbol had observable data",
                            )
                        ]
                    ),
                )
'''
if text.count(old) != 1:
    raise SystemExit("fully unavailable leader-construction anchor changed")
text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
