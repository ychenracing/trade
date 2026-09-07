from __future__ import annotations

import hashlib
from pathlib import Path

P24 = "f7400c0fff0718a0ac083f32f99b2fe96054647e"
IB24 = "f6aeb467b2aa70703a69a8b4053ec13aae90e2bc"
IS24 = "c10f748bd012eda3b1e3ab37f74ac734d76913ce"
R24 = "5204e90924533558a546f13a9c2f93b017898eb5"
W23 = "8db7981012416d6a8fe4c4394e68051d8c293336"
W23_REF = "codex/c6-v23-workflow-anchor"

P22 = "c50216f70cba21d830b6feb2079ed68039821ecf"
IB22 = "addb5c8ebf98ac43e08676cbc6ffe81a2627d9d7"
IS22 = "884851bdba4a5372c5272b28bd4df3e30b5824b9"
R22 = "ea6dfa36b9796df7e4078d6f8d88b539a54072e3"
R22_SHA = "44fe0934eae8c041ad2c7a2d001be85b7c16bf237f447a4addd1163e4f82ab18"
W21 = "f9da08afebf22b3dc03a1fb3a0ec351a79adf42c"
W21_REF = "codex/c6-v21-workflow-anchor"


def replace_required(path: Path, pairs: list[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in text:
            raise SystemExit(f"{path}: missing required token {old!r}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    r_path = Path("bindings/artifacts/diagnostics/c6-run-bindings.json")
    r_sha256 = hashlib.sha256(r_path.read_bytes()).hexdigest()

    replace_required(
        Path(".github/scripts/c6_auto_resume.py"),
        [(f'AUTO_ANCHOR = "{W21_REF}"', f'AUTO_ANCHOR = "{W23_REF}"')],
    )
    replace_required(
        Path(".github/workflows/c6-auto-resume.yml"),
        [
            (f"branches: [{W21_REF}]", f"branches: [{W23_REF}]"),
            ("group: c6-auto-resume-v21", "group: c6-auto-resume-v24"),
        ],
    )

    replace_required(
        Path(".github/scripts/c6_stage_advance.py"),
        [
            ("frozen C6 v22 stages", "frozen C6 v24 stages"),
            (f"P_COMMIT = '{P22}'", f"P_COMMIT = '{P24}'"),
            (f"BASE = '{IB22}'", f"BASE = '{IB24}'"),
            (f"S_SOURCE = '{IS22}'", f"S_SOURCE = '{IS24}'"),
            (f"R_COMMIT = '{R22}'", f"R_COMMIT = '{R24}'"),
            (f"R_SHA256 = '{R22_SHA}'", f"R_SHA256 = '{r_sha256}'"),
            (f"WORKFLOW = '{W21}'", f"WORKFLOW = '{W23}'"),
            (f"ANCHOR = '{W21_REF}'", f"ANCHOR = '{W23_REF}'"),
            ("D_REF = 'codex/c6-selection-v22'", "D_REF = 'codex/c6-selection-v24'"),
            ("EXECUTION_VERSION = 'v22'", "EXECUTION_VERSION = 'v24'"),
            ("codex/c6-preregistration-v22", "codex/c6-preregistration-v24"),
            ("codex/c6-base-v22", "codex/c6-base-v24"),
            ("codex/c6-s-v22", "codex/c6-s-v24"),
            ("codex/c6-evidence-v22", "codex/c6-evidence-v24"),
        ],
    )

    test_path = Path(".github/scripts/test_c6_stage_advance.py")
    test_text = test_path.read_text(encoding="utf-8")
    if "c6-v22-" not in test_text:
        raise SystemExit("stage relay tests have no v22 logical fixtures")
    test_text = test_text.replace("c6-v22-", "c6-v24-")
    test_path.write_text(test_text, encoding="utf-8")

    replace_required(
        Path(".github/workflows/c6-stage-advance.yml"),
        [
            ("codex/c6-stage-advance-v22", "codex/c6-stage-advance-v24"),
            ("c6-stage-advance-v22", "c6-stage-advance-v24"),
            (f"branches: [{W21_REF}]", f"branches: [{W23_REF}]"),
            (IB22, IB24),
            (R22, R24),
            ("C6 v22 frozen stage outcome", "C6 v24 frozen stage outcome"),
        ],
    )

    expected_paths = {
        ".github/scripts/c6_auto_resume.py",
        ".github/scripts/c6_stage_advance.py",
        ".github/scripts/test_c6_stage_advance.py",
        ".github/workflows/c6-auto-resume.yml",
        ".github/workflows/c6-stage-advance.yml",
    }
    for path in expected_paths:
        text = Path(path).read_text(encoding="utf-8")
        if "codex/c6-v24-workflow-anchor" in text:
            raise SystemExit(f"{path}: fabricated W24 workflow anchor")
    if W23_REF not in Path(".github/scripts/c6_auto_resume.py").read_text(encoding="utf-8"):
        raise SystemExit("auto-resume did not bind W23")
    stage = Path(".github/scripts/c6_stage_advance.py").read_text(encoding="utf-8")
    for token in (P24, IB24, IS24, R24, r_sha256, W23, W23_REF, "codex/c6-selection-v24", "EXECUTION_VERSION = 'v24'"):
        if token not in stage:
            raise SystemExit(f"stage relay missing v24 identity: {token}")


if __name__ == "__main__":
    main()
