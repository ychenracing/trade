from __future__ import annotations

from pathlib import Path

source_path = Path(__file__).with_name("build_c6_v21_anchor.py")
source = source_path.read_text(encoding="utf-8")

old_probe = 'if bound.count(old_probe) != 2:\n    raise SystemExit("probe anchor count drift")'
new_probe = 'if bound.count(old_probe) != 1:\n    raise SystemExit("probe anchor count drift")'
if source.count(old_probe) != 1:
    raise SystemExit("current adapter cannot locate the stale probe-count assertion")
source = source.replace(old_probe, new_probe, 1)

old_cli = "python -m quantfusion.application.c6_parallel_l1 \\\\"
new_cli = "python -m quantfusion.application.c6_parallel_l1_cli \\\\"
if source.count(old_cli) != 1:
    raise SystemExit("current adapter cannot locate the stale parallel CLI")
source = source.replace(old_cli, new_cli, 1)

exec(compile(source, str(source_path), "exec"), {"__name__": "__main__", "__file__": str(source_path)})
