from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path.cwd()
CANDIDATE = ROOT / "candidate"
STAGE = CANDIDATE / ".github/scripts/c6_stage_advance.py"
TEST = CANDIDATE / ".github/scripts/test_c6_stage_advance.py"
MAIN = "974a49d3d2763524d742bc0d5ed442d3acd65559"


def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=CANDIDATE, text=True)


def main() -> None:
    assert run("git", "rev-parse", "HEAD").strip() == MAIN
    stage = STAGE.read_text(encoding="utf-8")
    old = "                  'message': 'Seal mechanical C6 v19 selection: ' + selection['branch']})['sha']"
    new = "                  'message': selection_commit_message(selection['branch'])})['sha']"
    if stage.count(old) != 1:
        raise RuntimeError("stale selection message anchor missing/ambiguous")
    helper_anchor = "def rejection_reasons(branch):\n    reasons = {'BASE_REJECTED': 'BASE_L1_PREDICATE_FAILED',\n               'QUALIFICATION_REJECTED': 'S_QUALIFICATION_FAILED',\n               'BASE_PLUS_S_REJECTED': 'BASE_PLUS_S_L1_PREDICATE_FAILED'}\n    return [reasons[branch]] if branch in reasons else []\n\n\n"
    helper = helper_anchor + "def selection_commit_message(branch):\n    return f'Seal mechanical C6 {EXECUTION_VERSION} selection: {branch}'\n\n\n"
    if stage.count(helper_anchor) != 1:
        raise RuntimeError("selection helper insertion anchor missing/ambiguous")
    stage = stage.replace(helper_anchor, helper, 1).replace(old, new, 1)
    STAGE.write_text(stage, encoding="utf-8")

    test = TEST.read_text(encoding="utf-8")
    anchor = "    def test_malformed_predicates_and_ineligible_s_fail_closed(self):\n"
    addition = (
        "    def test_selection_commit_message_uses_current_execution_version(self):\n"
        "        message = relay.selection_commit_message('BASE_SELECTED')\n"
        "        self.assertEqual(message, 'Seal mechanical C6 v30 selection: BASE_SELECTED')\n"
        "        self.assertNotIn('v19', message)\n\n"
    )
    if test.count(anchor) != 1:
        raise RuntimeError("test insertion anchor missing/ambiguous")
    test = test.replace(anchor, addition + anchor, 1)
    TEST.write_text(test, encoding="utf-8")

    changed = run("git", "diff", "--name-only").splitlines()
    expected = [".github/scripts/c6_stage_advance.py", ".github/scripts/test_c6_stage_advance.py"]
    if changed != expected:
        raise RuntimeError(f"unexpected changed files: {changed}")
    subprocess.run(["git", "diff", "--check"], cwd=CANDIDATE, check=True)
    assert "Seal mechanical C6 v19 selection" not in STAGE.read_text(encoding="utf-8")
    assert "selection_commit_message(selection['branch'])" in STAGE.read_text(encoding="utf-8")
    print(run("git", "diff", "--", *expected))


if __name__ == "__main__":
    main()
