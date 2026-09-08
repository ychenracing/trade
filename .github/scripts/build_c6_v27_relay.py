from __future__ import annotations

import hashlib
import re
from pathlib import Path

P26 = "a56a97b56cf1479b46dcc258416255f6ac19b571"
IB26 = "2d7b6e2a153dcb3bc359f0ca75908e6a0c7e9631"
IS26 = "8c83a242481f510f3d5cba977de4628903011c62"
R26 = "85e7b3158f130e2f475de1bd67081ac3629739f1"
W26 = "f77d7d4f91e3816d5cde705cbb2f50255aa9200e"
W26_REF = "codex/c6-v26-workflow-anchor"

P27 = "e02089ec12c25a8912f2adf0cf9c53f3b844069b"
IB27 = "24682791cbe96acc69d36cd0dcfc42e69d61bcda"
IS27 = "d6b06596f3280d0e8ce3573d481cae99443ae866"
R27 = "1744434eac1c80bb1d74fa0d92fb13193aab9062"
W27 = "3a0eb31775d633b0a8dea431207c141ae1f290cc"
W27_REF = "codex/c6-v27-workflow-anchor"


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
        [(f'AUTO_ANCHOR = "{W26_REF}"', f'AUTO_ANCHOR = "{W27_REF}"')],
    )
    replace_required(
        Path(".github/workflows/c6-auto-resume.yml"),
        [
            (f"branches: [{W26_REF}]", f"branches: [{W27_REF}]"),
            ("group: c6-auto-resume-v26", "group: c6-auto-resume-v27"),
        ],
    )

    stage_path = Path(".github/scripts/c6_stage_advance.py")
    replace_required(
        stage_path,
        [
            ("frozen C6 v26 stages", "frozen C6 v27 stages"),
            (f"P_COMMIT = '{P26}'", f"P_COMMIT = '{P27}'"),
            (f"BASE = '{IB26}'", f"BASE = '{IB27}'"),
            (f"S_SOURCE = '{IS26}'", f"S_SOURCE = '{IS27}'"),
            (f"R_COMMIT = '{R26}'", f"R_COMMIT = '{R27}'"),
            (f"WORKFLOW = '{W26}'", f"WORKFLOW = '{W27}'"),
            (f"ANCHOR = '{W26_REF}'", f"ANCHOR = '{W27_REF}'"),
            ("D_REF = 'codex/c6-selection-v26'", "D_REF = 'codex/c6-selection-v27'"),
            ("EXECUTION_VERSION = 'v26'", "EXECUTION_VERSION = 'v27'"),
            ("codex/c6-preregistration-v26", "codex/c6-preregistration-v27"),
            ("codex/c6-base-v26", "codex/c6-base-v27"),
            ("codex/c6-s-v26", "codex/c6-s-v27"),
            ("codex/c6-evidence-v26", "codex/c6-evidence-v27"),
        ],
    )
    replace_one_regex(stage_path, r"R_SHA256 = '[0-9a-f]{64}'", f"R_SHA256 = '{r_sha256}'")

    test_path = Path(".github/scripts/test_c6_stage_advance.py")
    test_text = test_path.read_text(encoding="utf-8")
    if "c6-v26-" not in test_text:
        raise SystemExit("stage relay tests have no v26 logical fixtures")
    test_path.write_text(test_text.replace("c6-v26-", "c6-v27-"), encoding="utf-8")

    replace_required(
        Path(".github/workflows/c6-stage-advance.yml"),
        [
            ("codex/c6-stage-advance-v26", "codex/c6-stage-advance-v27"),
            ("c6-stage-advance-v26", "c6-stage-advance-v27"),
            (f"branches: [{W26_REF}]", f"branches: [{W27_REF}]"),
            (IB26, IB27),
            (R26, R27),
            ("C6 v26 frozen stage outcome", "C6 v27 frozen stage outcome"),
        ],
    )

    expected_paths = {
        ".github/scripts/c6_auto_resume.py",
        ".github/scripts/c6_stage_advance.py",
        ".github/scripts/test_c6_stage_advance.py",
        ".github/workflows/c6-auto-resume.yml",
        ".github/workflows/c6-stage-advance.yml",
    }
    if W27_REF not in Path(".github/scripts/c6_auto_resume.py").read_text(encoding="utf-8"):
        raise SystemExit("auto-resume did not bind W27")
    stage = stage_path.read_text(encoding="utf-8")
    for token in (P27, IB27, IS27, R27, r_sha256, W27, W27_REF, "codex/c6-selection-v27", "EXECUTION_VERSION = 'v27'"):
        if token not in stage:
            raise SystemExit(f"stage relay missing v27 identity: {token}")
    for path in expected_paths:
        text = Path(path).read_text(encoding="utf-8")
        if "c6-v26-" in text or "codex/c6-v26-workflow-anchor" in text:
            raise SystemExit(f"{path}: stale v26/W26 relay identity survived")


if __name__ == "__main__":
    main()
