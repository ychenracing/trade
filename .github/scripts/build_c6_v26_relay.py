from __future__ import annotations

import hashlib
import re
from pathlib import Path

P25 = "d6e76889e36f9efdc2685eb43aa57dff50ac1521"
IB25 = "f811cf8d0f0093b3148900bd0fbb96a9e67f9b51"
IS25 = "3d890d6d501c77a48a9ce46047d504b4f4838b46"
R25 = "71748de130d31d132e69999645e3285f81a651f9"
W23 = "8db7981012416d6a8fe4c4394e68051d8c293336"
W23_REF = "codex/c6-v23-workflow-anchor"

P26 = "a56a97b56cf1479b46dcc258416255f6ac19b571"
IB26 = "2d7b6e2a153dcb3bc359f0ca75908e6a0c7e9631"
IS26 = "8c83a242481f510f3d5cba977de4628903011c62"
R26 = "85e7b3158f130e2f475de1bd67081ac3629739f1"
W26 = "f77d7d4f91e3816d5cde705cbb2f50255aa9200e"
W26_REF = "codex/c6-v26-workflow-anchor"


def replace_required(path: Path, pairs: list[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in text:
            raise SystemExit(f"{path}: missing required token {old!r}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def replace_one_regex(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    text, count = re.subn(pattern, replacement, text)
    if count != 1:
        raise SystemExit(f"{path}: expected one regex replacement, got {count}: {pattern!r}")
    path.write_text(text, encoding="utf-8")


def main() -> None:
    r_path = Path("bindings/artifacts/diagnostics/c6-run-bindings.json")
    r_sha256 = hashlib.sha256(r_path.read_bytes()).hexdigest()

    replace_required(
        Path(".github/scripts/c6_auto_resume.py"),
        [(f'AUTO_ANCHOR = "{W23_REF}"', f'AUTO_ANCHOR = "{W26_REF}"')],
    )
    replace_required(
        Path(".github/workflows/c6-auto-resume.yml"),
        [
            (f"branches: [{W23_REF}]", f"branches: [{W26_REF}]"),
            ("group: c6-auto-resume-v25", "group: c6-auto-resume-v26"),
        ],
    )

    stage_path = Path(".github/scripts/c6_stage_advance.py")
    replace_required(
        stage_path,
        [
            ("frozen C6 v25 stages", "frozen C6 v26 stages"),
            (f"P_COMMIT = '{P25}'", f"P_COMMIT = '{P26}'"),
            (f"BASE = '{IB25}'", f"BASE = '{IB26}'"),
            (f"S_SOURCE = '{IS25}'", f"S_SOURCE = '{IS26}'"),
            (f"R_COMMIT = '{R25}'", f"R_COMMIT = '{R26}'"),
            (f"WORKFLOW = '{W23}'", f"WORKFLOW = '{W26}'"),
            (f"ANCHOR = '{W23_REF}'", f"ANCHOR = '{W26_REF}'"),
            ("D_REF = 'codex/c6-selection-v25'", "D_REF = 'codex/c6-selection-v26'"),
            ("EXECUTION_VERSION = 'v25'", "EXECUTION_VERSION = 'v26'"),
            ("codex/c6-preregistration-v25", "codex/c6-preregistration-v26"),
            ("codex/c6-base-v25", "codex/c6-base-v26"),
            ("codex/c6-s-v25", "codex/c6-s-v26"),
            ("codex/c6-evidence-v25", "codex/c6-evidence-v26"),
        ],
    )
    replace_one_regex(stage_path, r"R_SHA256 = '[0-9a-f]{64}'", f"R_SHA256 = '{r_sha256}'")

    test_path = Path(".github/scripts/test_c6_stage_advance.py")
    test_text = test_path.read_text(encoding="utf-8")
    if "c6-v25-" not in test_text:
        raise SystemExit("stage relay tests have no v25 logical fixtures")
    test_path.write_text(test_text.replace("c6-v25-", "c6-v26-"), encoding="utf-8")

    replace_required(
        Path(".github/workflows/c6-stage-advance.yml"),
        [
            ("codex/c6-stage-advance-v25", "codex/c6-stage-advance-v26"),
            ("c6-stage-advance-v25", "c6-stage-advance-v26"),
            (f"branches: [{W23_REF}]", f"branches: [{W26_REF}]"),
            (IB25, IB26),
            (R25, R26),
            ("C6 v25 frozen stage outcome", "C6 v26 frozen stage outcome"),
        ],
    )

    expected_paths = {
        ".github/scripts/c6_auto_resume.py",
        ".github/scripts/c6_stage_advance.py",
        ".github/scripts/test_c6_stage_advance.py",
        ".github/workflows/c6-auto-resume.yml",
        ".github/workflows/c6-stage-advance.yml",
    }
    if W26_REF not in Path(".github/scripts/c6_auto_resume.py").read_text(encoding="utf-8"):
        raise SystemExit("auto-resume did not bind W26")
    stage = stage_path.read_text(encoding="utf-8")
    for token in (P26, IB26, IS26, R26, r_sha256, W26, W26_REF, "codex/c6-selection-v26", "EXECUTION_VERSION = 'v26'"):
        if token not in stage:
            raise SystemExit(f"stage relay missing v26 identity: {token}")
    for path in expected_paths:
        text = Path(path).read_text(encoding="utf-8")
        if "c6-v25-" in text or "codex/c6-v23-workflow-anchor" in text:
            raise SystemExit(f"{path}: stale v25/W23 relay identity survived")


if __name__ == "__main__":
    main()
