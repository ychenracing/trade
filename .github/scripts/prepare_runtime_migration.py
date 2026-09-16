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
p.write_text(text.replace(old, new, 1), encoding='utf-8')
