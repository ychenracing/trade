from pathlib import Path

p = Path('.github/scripts/apply_research_runtime_boundary.py')
text = p.read_text(encoding='utf-8')
old = '''def one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)
'''
new = '''def one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if label in {"production runtime arg", "production runtime forwarding"} and count >= 1:
        return text.replace(old, new, 1)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)
'''
if text.count(old) != 1:
    raise SystemExit('runtime migration helper anchor changed')
text = text.replace(old, new, 1)

old_import_fallback = '''        if anchor not in text:
            anchor = "from unittest.mock import "
            pos = text.find(anchor)
            if pos < 0:
                # place after future import
                anchor = "from __future__ import annotations\\n"
                text = one(text, anchor, anchor + "\\nfrom quantfusion.research.c6_runtime import runtime_policy_for_intervention\\n", f"{name} runtime import")
            else:
                line_end = text.find("\\n", pos)
                text = text[:line_end+1] + "from quantfusion.research.c6_runtime import runtime_policy_for_intervention\\n" + text[line_end+1:]
'''
new_import_fallback = '''        if anchor not in text:
            anchor = "from unittest.mock import "
            pos = text.find(anchor)
            if pos >= 0:
                line_end = text.find("\\n", pos)
                text = text[:line_end+1] + "from quantfusion.research.c6_runtime import runtime_policy_for_intervention\\n" + text[line_end+1:]
            elif "from __future__ import annotations\\n" in text:
                anchor = "from __future__ import annotations\\n"
                text = one(text, anchor, anchor + "\\nfrom quantfusion.research.c6_runtime import runtime_policy_for_intervention\\n", f"{name} runtime import")
            elif "import pandas as pd\\n" in text:
                anchor = "import pandas as pd\\n"
                text = one(text, anchor, anchor + "from quantfusion.research.c6_runtime import runtime_policy_for_intervention\\n", f"{name} runtime import")
            else:
                text = "from quantfusion.research.c6_runtime import runtime_policy_for_intervention\\n" + text
'''
if text.count(old_import_fallback) != 1:
    raise SystemExit('runtime migration import fallback anchor changed')
text = text.replace(old_import_fallback, new_import_fallback, 1)

old_orchestration = '''text = one(
    text,
    "from quantfusion.engine.ensemble import PreparedSleeveRun, RunRequest\\n",
    "from quantfusion.engine.ensemble import PreparedSleeveRun, RunRequest\\nfrom quantfusion.engine.runtime import runtime_policy\\n",
    "orchestration runtime import",
)
'''
new_orchestration = '''text = one(
    text,
    "from quantfusion.engine.ensemble import PreparedSleeveRun, RunRequest\\n",
    "from quantfusion.engine.ensemble import PreparedSleeveRun, RunRequest\\n",
    "orchestration runtime import",
)
'''
if text.count(old_orchestration) != 1:
    raise SystemExit('orchestration runtime import anchor changed')
text = text.replace(old_orchestration, new_orchestration, 1)

old_formal_import = '''    "from quantfusion.engine.replay import ProductionReplayEngine\\nfrom quantfusion.research.c6_runtime import (\\n    c6_diagnostic_engine_config,\\n    runtime_policy_for_intervention,\\n    validate_c6_diagnostic_request,\\n)\\n",
'''
new_formal_import = '''    "from quantfusion.research.c6_runtime import (\\n    c6_diagnostic_engine_config,\\n    runtime_policy_for_intervention,\\n    validate_c6_diagnostic_request,\\n)\\n",
'''
if text.count(old_formal_import) != 1:
    raise SystemExit('formal research replacement anchor changed')
text = text.replace(old_formal_import, new_formal_import, 1)

formal_save = 'text = text.replace("ProductionReplayEngine.validate_c6_diagnostic_request(request)", "validate_c6_diagnostic_request(request)")\n'
formal_save_replacement = formal_save + '''text = one(
    text,
    "from quantfusion.engine.universe import BacktestEngine\\n",
    "",
    "formal obsolete engine import",
)
'''
if text.count(formal_save) != 1:
    raise SystemExit('formal validator migration anchor changed')
text = text.replace(formal_save, formal_save_replacement, 1)

old_feature_new = "new = '''    del BacktestEngine\n    for intervention, expected in (\n"
new_feature_new = "new = '''    for intervention, expected in (\n"
if text.count(old_feature_new) != 1:
    raise SystemExit('formal feature test anchor changed')
text = text.replace(old_feature_new, new_feature_new, 1)

p.write_text(text, encoding='utf-8')
