from __future__ import annotations
# ruff: noqa: E501
import hashlib
import json
import subprocess
from pathlib import Path
import pytest
from quantfusion.application.c6_contract import (
    ContractError,
    binding_identity,
    canonical_json_bytes,
    canonical_payload_hash,
    economic_tree_hash,
    load_preregistration,
    load_run_bindings,
    manifest_identity,
    render_bound_argv,
    strict_json_loads,
)
REPOSITORY = Path(__file__).resolve().parents[2]
PREREGISTRATION = REPOSITORY / "artifacts/diagnostics/c6-preregistration.json"


@pytest.mark.parametrize("branch", ["BASE_REJECTED", "BASE_SELECTED", "QUALIFICATION_REJECTED", "BASE_PLUS_S_REJECTED", "BASE_PLUS_S_SELECTED"])
def test_selection_shape_preserves_deferred_base_predicates(branch):
    from quantfusion.application import c6_contract as contract
    p = load_preregistration(PREREGISTRATION, repository=REPOSITORY)
    specs = p["diagnostic_predicate_manifests"]["L1_APPLICABLE_DIAGNOSTIC_PREDICATES"]
    rows = [{"predicate_id": x["id"], "passed": True} for x in specs]
    residuals = [] if branch == "BASE_SELECTED" else ["prefix-05"]
    if residuals:
        next(x for x in rows if x["predicate_id"] == "l1.mdd.noncanonical_18pct_screen")["passed"] = False
    if branch == "BASE_REJECTED":
        next(x for x in rows if x["predicate_id"] == "l1.correctness.synthetic_controls")["passed"] = False
    selected = branch in {"BASE_SELECTED", "BASE_PLUS_S_SELECTED"}
    qualified = branch.startswith("BASE_PLUS_S_")
    s_rows = [{"predicate_id": x["id"], "passed": True} for x in specs] if qualified else None
    if branch == "BASE_PLUS_S_REJECTED":
        s_rows[0]["passed"] = False
    reason = {"BASE_REJECTED": "BASE_L1_PREDICATE_FAILED", "QUALIFICATION_REJECTED": "S_QUALIFICATION_FAILED", "BASE_PLUS_S_REJECTED": "BASE_PLUS_S_L1_PREDICATE_FAILED"}
    state = {"branch": branch, "status": "selected" if selected else "rejected", "residual_ids": residuals,
             "base_l1_predicates": rows, "s_qualification": {} if branch not in {"BASE_SELECTED", "BASE_REJECTED"} else None,
             "base_plus_s_l1": {} if qualified else None, "base_plus_s_l1_predicates": s_rows,
             "selected_candidate": ("C6-Base" if branch == "BASE_SELECTED" else "C6-Base+S") if selected else None,
             "C": {} if selected else None, "rejection_reasons": [] if selected else [reason[branch]]}
    contract.validate_selection_shape(state, p)
    other_branches = [name for name in ("BASE_REJECTED", "BASE_SELECTED", "QUALIFICATION_REJECTED", "BASE_PLUS_S_REJECTED", "BASE_PLUS_S_SELECTED") if name != branch]
    for altered in [*({**state, "branch": name} for name in other_branches), {**state, "branch": "unknown"}, {**state, "branch": None}, {**state, "base_l1_predicates": rows[:-1]}, {**state, "status": "rejected" if selected else "selected"}]:
        with pytest.raises(ContractError):
            contract.validate_selection_shape(altered, p)
    if branch == "BASE_PLUS_S_SELECTED":
        rows[0]["passed"] = False  # identity is never a deferred predicate
        with pytest.raises(ContractError):
            contract.validate_selection_shape(state, p)

def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    with pytest.raises(ContractError, match="duplicate JSON key"):
        strict_json_loads('{"x":1,"x":2}')
    with pytest.raises(ContractError, match="non-finite"):
        strict_json_loads('{"x":NaN}')
    with pytest.raises(ContractError, match="non-finite"):
        canonical_json_bytes({"x": float("inf")})
    with pytest.raises(ContractError, match="object keys must be strings"):
        canonical_json_bytes({1: "x"})


def test_implementation_proofs_are_checked_against_real_git_objects(tmp_path):
    from copy import deepcopy
    from quantfusion.application.c6_contract import validate_implementation_git_proofs
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path).decode().strip()
    git('init', '-q')
    git('config', 'user.name', 'Synthetic Test')
    git('config', 'user.email', 'test@example.invalid')
    git('commit', '--allow-empty', '-qm', 'baseline')
    baseline = git('rev-parse', 'HEAD')
    def commit(path, text):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
        git('add', path)
        git('commit', '-qm', 'synthetic fixture')
        return {'commit': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}')}
    p = commit('P.json', '{}\n')
    base = commit('base.py', 'x = 1\n')
    selected = commit('s.py', 'y = 2\n')
    for value, parent, path in [(base,p,'base.py'), (selected,base,'s.py')]:
        value.update(comparison_base_commit=parent['commit'],comparison_base_tree=parent['tree'],
            first_parent_ancestor=True,merge_commit_count=0,changed_paths=[path],added_lines=1,deleted_lines=0,
            required_blobs={path:{'mode':'100644','git_blob':git('rev-parse',value['commit']+':'+path),
                'sha256':hashlib.sha256((tmp_path/path).read_bytes()).hexdigest()}})
    bindings = {'P':p,'implementations':{'I_B':base,'I_S':selected}}
    prereg = {'implementation_freeze':{key:value for alias,path in [('I_B','base.py'),('I_S','s.py')] for key,value in
        [(alias+'_allowed_paths',[path]),(alias+'_diff_budget',{'maximum_changed_paths':1,'maximum_added_lines':1,'maximum_deleted_lines':0})]}}
    validate_implementation_git_proofs(prereg,bindings,repository=tmp_path)
    for key,value in [('added_lines',0),('changed_paths',[]),('first_parent_ancestor',False),('tree',p['tree'])]:
        forged=deepcopy(bindings)
        forged['implementations']['I_B'][key]=value
        with pytest.raises(ContractError):
            validate_implementation_git_proofs(prereg,forged,repository=tmp_path)
    forged=deepcopy(bindings)
    forged['implementations']['I_S']['required_blobs']['s.py']['sha256']='0'*64
    with pytest.raises(ContractError,match='SHA-256'):
        validate_implementation_git_proofs(prereg,forged,repository=tmp_path)
    prereg['implementation_freeze']['I_B_diff_budget']['maximum_added_lines']=0
    with pytest.raises(ContractError,match='budget'):
        validate_implementation_git_proofs(prereg,bindings,repository=tmp_path)
    prereg['implementation_freeze']['I_B_diff_budget']['maximum_added_lines']=1
    prereg['implementation_freeze']['P_allowed_paths']=['P.json']
    prereg['authority']={'base_revision':baseline}
    git('switch', '--detach', p['commit'])
    r=commit('artifacts/diagnostics/c6-run-bindings.json','{}\n')
    validate_implementation_git_proofs(prereg,bindings,repository=tmp_path,bindings_revision=r['commit'])
    invalid=commit('extra.py','z=3\n')
    with pytest.raises(ContractError,match='evidence-only'):
        validate_implementation_git_proofs(prereg,bindings,repository=tmp_path,bindings_revision=invalid['commit'])
    prereg['implementation_freeze']['P_allowed_paths']=[]
    with pytest.raises(ContractError,match='P tree'):
        validate_implementation_git_proofs(prereg,bindings,repository=tmp_path,bindings_revision=r['commit'])
def test_canonical_payload_hash_uses_the_frozen_serialization() -> None:
    payload = {"z": [2, 1], "é": "值", "a": {"ok": True}}
    expected = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    assert canonical_json_bytes(payload) == expected
    assert canonical_payload_hash(payload) == hashlib.sha256(expected).hexdigest()
def test_manifest_identity_is_sorted_unique_and_lf_terminated() -> None:
    identity = manifest_identity(["z", "a"])
    assert identity == {
        "count": 2,
        "unique_count": 2,
        "sha256": hashlib.sha256(b"a\nz\n").hexdigest(),
    }
    with pytest.raises(ContractError, match="duplicate manifest ID"):
        manifest_identity(["same", "same"])
def test_preregistration_rejects_extra_root_keys(tmp_path: Path) -> None:
    prereg = {
        "schema_version": 1,
        "kind": "synthetic",
        "status": "frozen",
    }
    path = tmp_path / "P.json"
    path.write_text(json.dumps({**prereg, "surprise": True}), encoding="utf-8")
    with pytest.raises(ContractError, match="unexpected keys"):
        load_preregistration(
            path,
            expected_keys={"schema_version", "kind", "status"},
        )
def test_frozen_preregistration_version_matches_authority_branch() -> None:
    preregistration = load_preregistration(PREREGISTRATION, repository=REPOSITORY)
    assert preregistration["schema_version"] == 2
    prefix = "c6-causal-risk-closure-17x958-"
    experiment_id = preregistration["experiment_id"]
    assert experiment_id.startswith(prefix)
    version = experiment_id.removeprefix(prefix)
    assert version.startswith("v") and version[1:].isdigit()
    assert not version.startswith("v0")
    authority = preregistration["authority"]
    recovery = authority.get("recovery_continuation")
    if recovery is None:
        assert authority["branch"] == f"codex/c6-causal-risk-closure-{version}"
    else:
        # Recovery keeps PR 63 while immutable execution identities advance.
        assert authority["branch"] == "codex/c6-causal-risk-closure-v11"
        assert recovery["continuing_pull_request"] == 63
        assert recovery["old_P"] == "d3181f504de319daa9efa366e1f1faf727eab011"
        assert recovery["economic_hypotheses_changed"] is False
        assert recovery["governing_contract_path"] == "docs/C6_RECOVERY_CONTRACT.md"
        contract = REPOSITORY / recovery["governing_contract_path"]
        assert hashlib.sha256(contract.read_bytes()).hexdigest() == (
            recovery["governing_contract_sha256"]
        )
    assert len(preregistration["run_templates"]["binding_specs"]) == 7
    assert preregistration["checkpoint_and_lease_protocol"]["schema_version"] == 2
    definitions = preregistration["schema_catalog"]["definitions"]
    assert "base_evaluation_id" in definitions["common_prefix_comparison"]["exact_keys"]
    assert "base_evaluation_id" in definitions["no_effect_comparison"]["exact_keys"]
    if preregistration['schema_catalog']['schema_version'] == 2:
        assert definitions['no_effect_comparison']['wire_schema']['properties']['item_kind'] == {'const': 'evaluation'}
    else:
        assert definitions["no_effect_comparison"]["field_types"]["item_kind"] == "literal evaluation"
def test_bound_argv_accepts_only_declared_late_slots() -> None:
    template = ["python", "-m", "fixture", "--source", "{source_revision}"]
    assert render_bound_argv(
        template,
        {"source_revision": "a" * 40},
        allowed_slots={"source_revision"},
    ) == ["python", "-m", "fixture", "--source", "a" * 40]
    with pytest.raises(ContractError, match="unknown late-bound slot"):
        render_bound_argv(
            template,
            {"source_revision": "a" * 40, "threshold": "0.19"},
            allowed_slots={"source_revision"},
        )
def test_run_binding_has_an_exact_schema_and_self_consistent_identity(
    tmp_path: Path,
) -> None:
    p_identity = {
        "commit": "f" * 40,
        "tree": "1" * 40,
        "blob": "2" * 40,
        "sha256": "3" * 64,
    }
    workflow: dict[str, object] = {}
    implementations = {
        "I_B": {"commit": "a" * 40, "tree": "b" * 40, "required_blobs": {}},
        "I_S": {"commit": "c" * 40, "tree": "d" * 40, "required_blobs": {}},
    }
    binding = {
        "record_id": "c6.fixture.0",
        "workflow_binding_id": "c6.fixture",
        "candidate_id": "C6-Base",
        "logical_run_id": "fixture-0",
        "initial_attempt_id": "a0",
        "stage": "L1",
        "source_alias": "I_B",
        "source_revision": "a" * 40,
        "source_tree": "b" * 40,
        "source_blob_identities": {},
        "P": p_identity,
        "workflow": workflow,
        "entrypoint": "quantfusion.application.c6_diagnostics",
        "argv": ["python", "-m", "fixture", "--output", "{OUTPUT_PATH}"],
        "runtime_late_slots": ["OUTPUT_PATH"],
        "resolved_inputs": {},
        "scenario_manifest_identity": {
            "name": "FIXTURE_IDS",
            "count": 1,
            "unique_count": 1,
            "sha256": "d" * 64,
        },
        "synthetic_control_manifest_identity": None,
        "evaluation_manifest_identity": None,
        "item_manifest_contract": {},
        "producer_policy": "none",
        "decision_policy": "none",
        "runtime": {},
        "attempt_policy": {},
        "paths": {},
        "canonical_payload_schema": {},
        "exit_semantics": {},
        "record_signature": "",
    }
    record_ids = (
        "c6.base.l1", "c6.s.qualification", "c6.base_plus_s.l1",
        "c6.base.selected.l2", "c6.base_plus_s.selected.l2",
        "c6.base.selected.l4", "c6.base_plus_s.selected.l4",
    )
    records = []
    for index, record_id in enumerate(record_ids):
        candidate = "C6-Base" if record_id.startswith("c6.base.") else "C6-Base+S"
        alias = "I_B" if candidate == "C6-Base" else "I_S"
        implementation = implementations[alias]
        record = {
            **binding,
            "record_id": record_id,
            "logical_run_id": f"fixture-{index}",
            "candidate_id": candidate,
            "source_alias": alias,
            "source_revision": implementation["commit"],
            "source_tree": implementation["tree"],
        }
        record["record_signature"] = binding_identity(record)
        records.append(record)
    payload = {
        "schema_version": 2,
        "kind": "c6_run_bindings",
        "status": "frozen_before_economic_dispatch",
        "P": p_identity,
        "workflow": workflow,
        "implementations": implementations,
        "binding_records": records,
        "cross_record_invariants": {},
        "selection_validator": {},
        "serialization": "canonical_json_v2",
    }
    path = tmp_path / "R.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_run_bindings(path, allowed_late_slots={"OUTPUT_PATH"}) == payload
    payload["binding_records"][0]["threshold"] = 0.19
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="unexpected keys"):
        load_run_bindings(path, allowed_late_slots={"OUTPUT_PATH"})
    with pytest.raises(ContractError, match="undeclared placeholder"):
        render_bound_argv(
            ["python", "{threshold}"],
            {"threshold": "0.19"},
            allowed_slots={"source_revision"},
        )
def test_economic_tree_hash_uses_exact_git_records_and_allowlist(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True
    )
    (tmp_path / "economic.txt").write_text("fixed\n", encoding="utf-8")
    (tmp_path / "report.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=tmp_path, check=True)
    first = economic_tree_hash("HEAD", ["report.txt"], repository=tmp_path)
    (tmp_path / "report.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "report"], cwd=tmp_path, check=True)
    assert economic_tree_hash("HEAD", ["report.txt"], repository=tmp_path) == first
    (tmp_path / "economic.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "economic"], cwd=tmp_path, check=True)
    assert economic_tree_hash("HEAD", ["report.txt"], repository=tmp_path) != first
    with pytest.raises(ContractError, match="repo-relative"):
        economic_tree_hash("HEAD", ["../report.txt"], repository=tmp_path)


def test_selection_producer_payloads_cannot_inherit_forged_predicates_or_residuals():
    from copy import deepcopy
    from quantfusion.application import c6_contract as contract
    rows = [{'predicate_id': 'gate', 'passed': True}]
    selection = {'base_l1_predicates': rows, 'residual_ids': [], 's_qualification': None,
                 'base_plus_s_l1': None, 'base_plus_s_l1_predicates': None}
    payload = {'evaluations': [{'variant_id': 'C6-Base', 'scenario_id': 'a',
                               'official_metrics': {'max_drawdown': -.1}}],
               'diagnostic_predicates': rows}
    contract.validate_selection_producer_payloads(selection, payload, None, None, ['a'])
    for key, value in [('base_l1_predicates', [{'predicate_id': 'gate', 'passed': False}]), ('residual_ids', ['a'])]:
        altered = {**selection, key: value}
        with pytest.raises(ContractError, match='sealed Base'):
            contract.validate_selection_producer_payloads(altered, payload, None, None, ['a'])
    altered = deepcopy(payload)
    altered['evaluations'][0]['scenario_id'] = 'unknown'
    with pytest.raises(ContractError, match='sealed Base'):
        contract.validate_selection_producer_payloads(selection, altered, None, None, ['a'])


def test_selection_requires_actual_qualified_s_chain():
    from copy import deepcopy
    from quantfusion.application import c6_contract as contract
    identity = {'artifact_full_byte_sha256': 'a' * 64, 'attempt_id': 'a0', 'record_id': 'c6.base.l1',
                'logical_run_id': 'base', 'workflow_run_id': '123'}
    producer = {k: v for k, v in identity.items() if k != 'record_id'} | {'binding_id': 'c6.base.l1'}
    q_identity = {**identity, 'record_id': 'c6.s.qualification', 'logical_run_id': 'qualification', 'workflow_run_id': '124'}
    q_producer = {k: v for k, v in q_identity.items() if k != 'record_id'} | {'binding_id': 'c6.s.qualification'}
    base_rows = [{'predicate_id': 'mdd', 'passed': False}]
    s_rows = [{'predicate_id': 'mdd', 'passed': True}]
    selection = {'branch': 'BASE_PLUS_S_SELECTED', 'base_l1': identity, 'base_l1_predicates': base_rows,
                 'residual_ids': ['a'], 's_qualification': q_identity,
                 'base_plus_s_l1': {}, 'base_plus_s_l1_predicates': s_rows}
    base = {'evaluations': [{'variant_id': 'C6-Base', 'scenario_id': 'a', 'official_metrics': {'max_drawdown': -.19}}],
            'diagnostic_predicates': base_rows}
    qualification = {'base_producer_identity': producer, 'residual_ids': ['a'], 'all_passed': True,
                     'results': [{'scenario_id': 'a', 'passed': True, 'criteria': [{'passed': True} for _ in range(7)]}]}
    selected = {'diagnostic_predicates': s_rows, 'base_producer_identity': producer, 'qualification_producer_identity': q_producer}
    contract.validate_selection_producer_payloads(selection, base, qualification, selected, ['a'])
    for mutation in ('qualification', 'nested', 'link', 's_predicates'):
        q, s = deepcopy(qualification), deepcopy(selected)
        if mutation == 'qualification':
            q['all_passed'] = False
        elif mutation == 'nested':
            q['results'][0]['criteria'][0]['passed'] = False
        elif mutation == 'link':
            s['base_producer_identity']['workflow_run_id'] = '999'
        else:
            s['diagnostic_predicates'][0]['passed'] = False
        with pytest.raises(ContractError, match='sealed'):
            contract.validate_selection_producer_payloads(selection, base, q, s, ['a'])
