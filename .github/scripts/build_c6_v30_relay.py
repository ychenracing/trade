from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path.cwd()
CANDIDATE = ROOT / "candidate"
OUT = ROOT / "relay-output"
MAIN = "ae977737b11aff0054371984dd348e5f2db9d458"
P30 = "358f484bdae6570c149c4677937d27fa888c4ce3"
IB30 = "dc1553f040695ed18379e52a04889de25f3c11f6"
IS30 = "7ca9bdede4e5db408e38cfe8ab3a165b384b9a22"
R30 = "6bd92daca464e803920de51a42375728963e74e9"
R30_SHA256 = "a42fe293146ae0f4d64a4e157132ee967a528df3b11a55dbd47e3b65b396194a"
W30 = "927b2dd86225769db5c22a98af06f4244fa6f448"

FILES = [
    ".github/scripts/c6_auto_resume.py",
    ".github/scripts/c6_stage_advance.py",
    ".github/scripts/test_c6_stage_advance.py",
    ".github/scripts/test_c6_stage_artifacts.py",
    ".github/workflows/c6-auto-resume.yml",
    ".github/workflows/c6-stage-advance.yml",
]

SHA_REPLACEMENTS = {
    "8eaca66fc1f617e2a7f52caa422f99d6c3a8729f": P30,
    "dfed1e6584b0b0e039bd720ff6eff32348123eab": IB30,
    "60d42b3931ccc7319a7cc815d363b9950e2586d7": IS30,
    "9e7e77c37dcf8a1531c223238bfc48cdcfd45ee4": R30,
    "729b48e0a0fff0b3d09ae8f4651dbaf54fce5edd7e24299957962f258dbcff98": R30_SHA256,
    "01fb58613b44aee921c43291fe56203e76b52b0c": W30,
}


def run(*args: str, cwd: Path = CANDIDATE) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True)


def main() -> None:
    assert run("git", "rev-parse", "HEAD").strip() == MAIN
    OUT.mkdir(exist_ok=True)
    for path in FILES:
        target = CANDIDATE / path
        original = target.read_text(encoding="utf-8")
        text = original.replace("v29", "v30")
        for old, new in SHA_REPLACEMENTS.items():
            text = text.replace(old, new)
        if text == original:
            raise RuntimeError(f"expected v30 relay change missing: {path}")
        target.write_text(text, encoding="utf-8")

    changed = run("git", "diff", "--name-only").splitlines()
    assert changed == FILES, changed
    subprocess.run(["git", "diff", "--check"], cwd=CANDIDATE, check=True)

    auto = (CANDIDATE / FILES[0]).read_text()
    stage = (CANDIDATE / FILES[1]).read_text()
    assert 'AUTO_ANCHOR = "codex/c6-v30-workflow-anchor"' in auto
    for expected in (P30, IB30, IS30, R30, R30_SHA256, W30, "codex/c6-selection-v30", "EXECUTION_VERSION = 'v30'"):
        assert expected in stage, expected
    assert "v29" not in auto
    assert "v29" not in stage

    for path in FILES:
        src = CANDIDATE / path
        dst = OUT / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    diff = run("git", "diff", "--", *FILES)
    (OUT / "candidate.diff").write_text(diff, encoding="utf-8")
    metadata = {
        "base_main": MAIN,
        "files": FILES,
        "P30": P30,
        "I_B30": IB30,
        "I_S30": IS30,
        "R30": R30,
        "R30_sha256": R30_SHA256,
        "W30": W30,
        "economic_changes": 0,
        "scheduler_count_changed": False,
        "schedules_changed": False,
        "file_sha256": {path: hashlib.sha256((CANDIDATE / path).read_bytes()).hexdigest() for path in FILES},
    }
    (OUT / "candidate.json").write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
