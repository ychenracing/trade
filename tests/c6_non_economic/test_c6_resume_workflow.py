from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "c6-bound-economic.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def test_resume_preflight_branch_keeps_economic_tests_embargoed() -> None:
    assert "codex/c6-resume-preflight" in CI.read_text(encoding="utf-8")


def test_resume_uses_explicit_monotonic_fence_and_prior_artifact() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for token in (
        "fencing_sequence:",
        "prior_artifact_id:",
        "C6_FENCING_SEQUENCE: ${{ inputs.fencing_sequence }}",
        "C6_PRIOR_ARTIFACT_ID: ${{ inputs.prior_artifact_id }}",
        '--fencing-sequence "$C6_FENCING_SEQUENCE"',
        '--prior-artifact-id "$C6_PRIOR_ARTIFACT_ID"',
        'test "$C6_FENCING_SEQUENCE" = "1"',
        'test "$C6_FENCING_SEQUENCE" -gt 1',
    ):
        assert token in workflow


def test_artifact_identity_includes_fencing_sequence() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert (
        "name: c6-bound-${{ inputs.logical_run_id }}-"
        "${{ inputs.attempt_id }}-${{ inputs.fencing_sequence }}"
    ) in workflow
