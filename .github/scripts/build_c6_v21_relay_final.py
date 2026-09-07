from __future__ import annotations

import os
from pathlib import Path

import yaml

P21 = os.environ["P21"]
IB21 = os.environ["IB21"]
IS21 = os.environ["IS21"]
R21 = os.environ["R21"]
W21 = os.environ["W21"]
R_SHA = os.environ["R_SHA256"]


def replace_all_required(path: str, replacements: list[tuple[str, str]]) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"{path}: missing required legacy identity {old!r}")
        text = text.replace(old, new)
    target.write_text(text, encoding="utf-8")


replace_all_required(
    ".github/scripts/c6_auto_resume.py",
    [("codex/c6-v17-workflow-anchor", "codex/c6-v21-workflow-anchor")],
)
replace_all_required(
    ".github/workflows/c6-auto-resume.yml",
    [
        ("codex/c6-v17-workflow-anchor", "codex/c6-v21-workflow-anchor"),
        ("c6-auto-resume-v17", "c6-auto-resume-v21"),
    ],
)
replace_all_required(
    ".github/scripts/c6_stage_advance.py",
    [
        ("3611ac948287ee147ed40e77eda8ea01bab27a35", P21),
        ("168cd3856cc1b60c928fe54c4a826b6045df01e9", IB21),
        ("00485e8ceb67f4105d76fa62368f583ce82a81c5", IS21),
        ("4db5cadc90d450f513dfdda10e0436cf330c62f3", R21),
        ("9c7fb1a92d8e04c5b95d1d18503ccb4e313f3dde6a3fa73fe6f92e24830956ac", R_SHA),
        ("d8bc65f3edaf1869e0c6c26ba9f26d7e7931ced4", W21),
        ("codex/c6-v17-workflow-anchor", "codex/c6-v21-workflow-anchor"),
        ("codex/c6-selection-v19", "codex/c6-selection-v21"),
        ("codex/c6-preregistration-v19", "codex/c6-preregistration-v21"),
        ("codex/c6-base-v19", "codex/c6-base-v21"),
        ("codex/c6-s-v19", "codex/c6-s-v21"),
        ("codex/c6-evidence-v19", "codex/c6-evidence-v21"),
        ("EXECUTION_VERSION = 'v19'", "EXECUTION_VERSION = 'v21'"),
        ("v19 stages", "v21 stages"),
    ],
)
replace_all_required(
    ".github/workflows/c6-stage-advance.yml",
    [
        ("codex/c6-stage-advance-v19", "codex/c6-stage-advance-v21"),
        ("codex/c6-v17-workflow-anchor", "codex/c6-v21-workflow-anchor"),
        ("c6-stage-advance-v19", "c6-stage-advance-v21"),
        ("168cd3856cc1b60c928fe54c4a826b6045df01e9", IB21),
        ("4db5cadc90d450f513dfdda10e0436cf330c62f3", R21),
        ("C6 v19 frozen stage outcome", "C6 v21 frozen stage outcome"),
    ],
)

test_path = Path(".github/scripts/test_c6_stage_advance.py")
test_text = test_path.read_text(encoding="utf-8")
if "c6-v19-" not in test_text:
    raise SystemExit("stage relay fixture has no v19 marker to rebind")
test_path.write_text(test_text.replace("c6-v19-", "c6-v21-"), encoding="utf-8")

for workflow in (
    ".github/workflows/c6-auto-resume.yml",
    ".github/workflows/c6-stage-advance.yml",
):
    yaml.safe_load(Path(workflow).read_text(encoding="utf-8"))
