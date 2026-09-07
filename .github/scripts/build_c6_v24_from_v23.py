from __future__ import annotations

from pathlib import Path

OLD_PR_HEAD = "5cede8cd8916f33fbf701d454a3a939a8352c0d3"
NEW_PR_HEAD = "7da0f7bfac490e022eba10a945119a3a8b90c1bc"
W23_REF = "codex/c6-v23-workflow-anchor"
P23 = "2cfe3775d12f8e4dea0b82220b45c8a4598fb2a9"
IB23 = "322c3fce34eff6ce0e2ca12b2c455178257358ed"
IS23 = "b2007568b84d5abea224c3d316794d15509fc366"
R23 = "fc72ae96b6c03418e35046b5a377ef5a02f5d27b"


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"expected one builder token, got {text.count(old)}: {old!r}")
    return text.replace(old, new)


def main() -> None:
    source = Path(".github/scripts/build_c6_v23_freeze.py")
    text = source.read_text(encoding="utf-8")

    text = replace_once(
        text,
        f'PR_HEAD = "{OLD_PR_HEAD}"',
        f'PR_HEAD = "{NEW_PR_HEAD}"',
    )
    text = replace_once(
        text,
        'p["frozen_at"] = "2026-09-07T17:32:30Z"',
        'p["frozen_at"] = "2026-09-07T17:50:00Z"',
    )

    placeholder = "__C6_RETAIN_W23_WORKFLOW_REF__"
    if text.count(W23_REF) < 2:
        raise SystemExit("W23 workflow ref is unexpectedly absent from v23 builder")
    text = text.replace(W23_REF, placeholder)
    text = text.replace("v23", "v24")
    text = text.replace(placeholder, W23_REF)

    constants_anchor = 'R22 = "ea6dfa36b9796df7e4078d6f8d88b539a54072e3"\n'
    constants = constants_anchor + (
        f'P23 = "{P23}"\n'
        f'IB23 = "{IB23}"\n'
        f'IS23 = "{IS23}"\n'
        f'R23 = "{R23}"\n'
    )
    text = replace_once(text, constants_anchor, constants)

    expected_anchor = '        "refs/heads/codex/c6-evidence-v22": R22,\n'
    expected = expected_anchor + (
        '        "refs/heads/codex/c6-preregistration-v23": P23,\n'
        '        "refs/heads/codex/c6-base-v23": IB23,\n'
        '        "refs/heads/codex/c6-s-v23": IS23,\n'
        '        "refs/heads/codex/c6-evidence-v23": R23,\n'
    )
    text = replace_once(text, expected_anchor, expected)

    metadata_anchor = '        "prior_frozen_identities": {\n'
    metadata = (
        '        "superseded_predispatch_v23": {\n'
        '            "P": P23,\n'
        '            "I_B": IB23,\n'
        '            "I_S": IS23,\n'
        '            "R": R23,\n'
        '            "economic_dispatches": 0,\n'
        '            "reason": "PR63 received only EOF-newline CI-hygiene commits after v23 freeze and before any v23 dispatch; immutable v23 refs are retained but not executed.",\n'
        '            "changed_paths": [\n'
        '                "quantfusion/application/c6_parallel_l1.py",\n'
        '                "tests/c6_non_economic/test_c6_parallel_l1_attestation.py",\n'
        '            ],\n'
        '            "economic_runtime_semantics_changed": False,\n'
        '        },\n'
        + metadata_anchor
    )
    text = replace_once(text, metadata_anchor, metadata)

    if NEW_PR_HEAD not in text or OLD_PR_HEAD in text:
        raise SystemExit("v24 builder PR head replacement failed")
    for stale in (
        "refs/heads/codex/c6-preregistration-v23\": p_sha",
        "refs/heads/codex/c6-base-v23\": ib_sha",
        "refs/heads/codex/c6-s-v23\": is_sha",
        "refs/heads/codex/c6-evidence-v23\": r_sha",
    ):
        if stale in text:
            raise SystemExit(f"v23 output target survived: {stale}")
    for required in (
        "c6-causal-risk-closure-17x958-v24",
        "refs/heads/codex/c6-preregistration-v24",
        "refs/heads/codex/c6-base-v24",
        "refs/heads/codex/c6-s-v24",
        "refs/heads/codex/c6-evidence-v24",
        W23_REF,
        P23,
        IB23,
        IS23,
        R23,
    ):
        if required not in text:
            raise SystemExit(f"missing v24 builder identity: {required}")

    output = Path("/tmp/build_c6_v24_freeze.py")
    output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
