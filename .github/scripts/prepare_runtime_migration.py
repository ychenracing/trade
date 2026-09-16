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
p.write_text(text, encoding='utf-8')
