"""One-shot C6 interruption rebinding; never executes economics or moves remote refs."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
from quantfusion.application.c6_contract import (
    binding_identity, canonical_json_bytes, load_run_bindings,
    validate_preregistration, validate_implementation_git_proofs,
)

P_PATH = 'artifacts/diagnostics/c6-preregistration.json'
R_PATH = 'artifacts/diagnostics/c6-run-bindings.json'
OLD = {
    'P': '1a66eab853561609fe9ca3e060157abd77c915b9',
    'I_B': '0679e65aa269ecca46c038b7bfea785892c969e2',
    'I_S': 'c4fa73a00b41eaf88c4d738e514eb0d930007457',
    'R': 'b5a3cfecef70957854a2f4cc751d67b63d1b25a9',
}
MAIN = 'd8bc65f3edaf1869e0c6c26ba9f26d7e7931ced4'
PR_HEAD = 'ddab7d6cc16ebe307211fdc218896508fe7e26cd'
FROZEN_AT = '2026-09-06T01:15:31Z'
ENV = os.environ | {
    'GIT_AUTHOR_NAME': 'ychenracing', 'GIT_AUTHOR_EMAIL': 'ychenracing@163.com',
    'GIT_COMMITTER_NAME': 'ychenracing', 'GIT_COMMITTER_EMAIL': 'ychenracing@163.com',
    'GIT_AUTHOR_DATE': FROZEN_AT, 'GIT_COMMITTER_DATE': FROZEN_AT,
}

def git(*args: str, data: bytes | None = None, env: dict | None = None) -> bytes:
    return subprocess.run(['git', *args], cwd=ROOT, input=data, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=ENV if env is None else env).stdout

def text(*args: str) -> str:
    return git(*args).decode().strip()

def blob_identity(rev: str, path: str) -> dict:
    mode, kind, oid = text('ls-tree', rev, '--', path).split()[:3]
    assert mode == '100644' and kind == 'blob', (rev, path)
    return {'mode': mode, 'git_blob': oid,
            'sha256': hashlib.sha256(git('show', f'{rev}:{path}')).hexdigest()}

def overlay_tree(rev: str, path: str, raw: bytes) -> str:
    oid = git('hash-object', '-w', '--stdin', data=raw).decode().strip()
    with tempfile.TemporaryDirectory(prefix='c6-v19-index-') as tmp:
        env = ENV | {'GIT_INDEX_FILE': str(Path(tmp) / 'index')}
        git('read-tree', rev, env=env)
        git('update-index', '--add', '--cacheinfo', f'100644,{oid},{path}', env=env)
        return git('write-tree', env=env).decode().strip()

def commit(tree: str, parents: list[str], message: str) -> str:
    argv = ['commit-tree', tree]
    for parent in parents:
        argv += ['-p', parent]
    return git(*argv, data=(message + '\n').encode()).decode().strip()

def changed_paths(a: str, b: str) -> list[str]:
    return sorted([x.decode() for x in git('diff', '--name-only', '-z', '--no-renames', a, b).split(b'\0') if x], key=lambda x: x.encode())

def implementation(rev: str, base: str) -> dict:
    paths = changed_paths(base, rev)
    added = deleted = 0
    for line in git('diff', '--numstat', '-z', '--no-renames', base, rev).split(b'\0'):
        if line:
            plus, minus, _ = line.split(b'\t', 2)
            assert plus.isdigit() and minus.isdigit()
            added += int(plus); deleted += int(minus)
    return {'commit': rev, 'tree': text('rev-parse', rev+'^{tree}'),
            'required_blobs': {p: blob_identity(rev, p) for p in paths},
            'comparison_base_commit': base, 'comparison_base_tree': text('rev-parse', base+'^{tree}'),
            'first_parent_ancestor': True, 'merge_commit_count': 0,
            'changed_paths': paths, 'added_lines': added, 'deleted_lines': deleted}

assert text('rev-parse', 'HEAD') == PR_HEAD
assert not text('status', '--porcelain', '-uall')
old_p_raw = git('show', f"{OLD['P']}:{P_PATH}")
old_r_raw = git('show', f"{OLD['R']}:{R_PATH}")
assert hashlib.sha256(old_r_raw).hexdigest() == 'ab7dc6d357162829679cb37f5e05aeb7fd26d49e76a09799900d3253eb117833'
old_p = json.loads(old_p_raw)
old_r_file = OUT / 'old-run-bindings.json'
old_r_file.write_bytes(old_r_raw)
old_r = load_run_bindings(old_r_file)
validate_preregistration(old_p, repository=ROOT)
validate_implementation_git_proofs(old_p, old_r, repository=ROOT, bindings_revision=OLD['R'])
assert old_p['authority']['base_revision'] == MAIN
assert old_p['experiment_id'] == 'c6-causal-risk-closure-17x958-v18'
base_ci_differences = changed_paths('e5ba9922ecac3a3797917225c3160f8e01c4abd6', OLD['I_B'])
assert all(path == P_PATH or path == 'AGENTS.md' or path.startswith('.github/') for path in base_ci_differences), base_ci_differences
assert text('rev-parse', PR_HEAD+'^{tree}') == text('rev-parse', OLD['I_S']+'^{tree}')
assert hashlib.sha256(git('show', PR_HEAD+':docs/C6_RECOVERY_CONTRACT.md')).hexdigest() == old_p['authority']['recovery_continuation']['governing_contract_sha256']

p = copy.deepcopy(old_p)
p['experiment_id'] = 'c6-causal-risk-closure-17x958-v19'
p['frozen_at'] = FROZEN_AT
p['authority']['recovery_continuation']['operational_interruption'] = {
    'class': 'D_user_requested_interruption_with_A_identity_rebinding',
    'authorization': 'TRADE_C6_HANDOFF_PROMPT_20260906(1).md; existing recovery contract A/D; no new strategy research',
    'handoff_file_sha256': 'c280372713f8cea1a9d8c9082d2b51c702cdafff2051180921636c24e042b961',
    'preserved_execution_revision': 'v18',
    'preserved_identities': OLD,
    'cancelled_workflow_run_id': 33976099202,
    'cancelled_logical_run_id': 'c6-v18-base-l1',
    'cancelled_attempt_id': 'a0',
    'terminal_status': 'completed/cancelled',
    'sealed_artifact_count': 0,
    'available_sealed_completed_items': 0,
    'executed_but_unsealed_item_count': None,
    'classification': 'incomplete operational interruption; neither valid economic rejection nor a terminal accepted result',
    'recovery_receipt_run_id': 34003295122,
    'recovery_receipt_artifact_id': 9980136292,
    'recovery_receipt_archive_sha256': '9788c63448a0b49db795f9875847c98361fd7be5938afba5d9addc423679bd09',
    'protocol_reason': 'GitHubActionsLeaseStore.restore requires one exact sealed artifact for each prior logical attempt; the cancelled v18 attempt has none. Native rerun, same-logical a0 reuse and fabricated resume/checkpoint are prohibited.',
    'new_execution_revision': 'v19',
    'economic_hypotheses_changed': False,
    'source_code_tests_data_dependencies_workflow_unchanged': True,
    'new_source_identity': 'P metadata changes require real independent I_B/I_S commits; actual commit/tree identities are bound by R, never relabelled as v18.',
    'old_v11_imported_records': 0,
    'v18_imported_records': 0,
    'base_execution_items_to_compute': 3875,
    'base_economic_evaluations_to_compute': 3831,
    'diagnostic_unique_scenarios': 765,
    'old_v11_prefix_records_to_recompute': 530,
    'recomputation_completed_at_freeze': 0,
    'engineering_evidence_reuse': {
        'Base_run_id': 33974140029, 'Base_source_revision': 'e5ba9922ecac3a3797917225c3160f8e01c4abd6',
        'S_exact_integration_run_id': 33975892852, 'S_source_revision': PR_HEAD,
        'main_run_id': 33975521834,
        'method': 'Pairwise v18-to-v19 tracked trees differ only at preregistration JSON. Base production/tests/data/lock match Base CI; subsequent workflow and AGENTS changes are covered by the retained main/PR CI. Validate new P/R and independent-source diff proofs. New PR HEAD still requires current exact-HEAD CI.',
        'cloud_resume_proof': 'Reuse unchanged v17 cross-version chain and artifact9972209195; no redundant probe.',
    },
    'selection': 'Fixed Base residuals -> all-residual S qualification if needed -> eligible S L1 -> mechanical D -> selected L2 -> official17/958; no third candidate or tuning.',
}
workflow = p['run_templates']['workflow']
workflow['trusted_source_refs'] = {'I_B': 'codex/c6-base-v19', 'I_S': 'codex/c6-s-v19'}
workflow['trusted_evidence_ref'] = 'codex/c6-evidence-v19'
for spec in p['run_templates']['binding_specs']:
    assert spec['logical_run_id'].startswith('c6-v18-')
    spec['logical_run_id'] = spec['logical_run_id'].replace('c6-v18-', 'c6-v19-', 1)
fields = p['run_templates']['R_schema']['R_cross_record_invariants']['field_types']
for key in ('trusted_ref_by_source_alias', 'trusted_evidence_ref'):
    assert 'v18' in fields[key]
    fields[key] = fields[key].replace('v18', 'v19')
for row in p['workflow_trigger_matrix']:
    if row.get('ref') in ('codex/c6-base-v18', 'codex/c6-s-v18', 'codex/c6-evidence-v18'):
        row['ref'] = row['ref'].replace('v18', 'v19')
# All economic semantics and prior historical proof are literally preserved.
for key in p:
    if key not in {'experiment_id', 'frozen_at', 'authority', 'run_templates', 'workflow_trigger_matrix'}:
        assert p[key] == old_p[key], key
assert {k:v for k,v in p['authority']['recovery_continuation'].items() if k != 'operational_interruption'} == old_p['authority']['recovery_continuation']
validate_preregistration(p, repository=ROOT)
p_raw = (json.dumps(p, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()
(OUT/'c6-preregistration.json').write_bytes(p_raw)
P = commit(overlay_tree(OLD['P'], P_PATH, p_raw), [OLD['P']],
           'C6 v19 preregistration: recover cancelled v18 without sealed checkpoint\n\nOperational identity amendment only; preserve v18, economic formulas, source blobs, gates, data and workflow. New logical execution, no native rerun or fake checkpoint. Continue PR63 under the authorized recovery contract.')
B = commit(overlay_tree(OLD['I_B'], P_PATH, p_raw), [P],
           'Freeze independent C6-Base v19 with unchanged v18 implementation\n\nOnly preregistration/operational identity differs from I_B v18; no S production code.')
S = commit(overlay_tree(OLD['I_S'], P_PATH, p_raw), [B],
           'Freeze independent conditional C6-S v19 with unchanged v18 implementation\n\nS remains unselected and cannot run until full Base residual qualification passes.')
for alias, rev in [('I_B',B), ('I_S',S)]:
    assert changed_paths(OLD[alias], rev) == [P_PATH], alias
    assert git('diff', '--raw', '--no-abbrev', OLD[alias], rev).decode().endswith(' M\t'+P_PATH+'\n')
pi = blob_identity(P, P_PATH)
p_identity = {'commit':P,'tree':text('rev-parse',P+'^{tree}'),'blob':pi['git_blob'],'sha256':pi['sha256']}
r = copy.deepcopy(old_r)
r['P'] = p_identity
r['implementations'] = {'I_B':implementation(B,P), 'I_S':implementation(S,B)}
for alias in ('I_B','I_S'):
    for field in ('required_blobs','changed_paths','added_lines','deleted_lines'):
        assert r['implementations'][alias][field] == old_r['implementations'][alias][field], (alias,field)
for record, spec in zip(r['binding_records'], p['run_templates']['binding_specs'], strict=True):
    assert record['record_id'] == spec['record_id']
    source = r['implementations'][spec['source_alias']]
    record.update(P=p_identity, logical_run_id=spec['logical_run_id'], source_revision=source['commit'],
                  source_tree=source['tree'], source_blob_identities=source['required_blobs'],
                  argv=[v.replace('{SOURCE_REVISION}',source['commit']) for v in spec['argv_template']])
    record['record_signature'] = binding_identity(record)
r['cross_record_invariants']['trusted_ref_by_source_alias'] = workflow['trusted_source_refs']
r['cross_record_invariants']['trusted_evidence_ref'] = workflow['trusted_evidence_ref']
r_raw = canonical_json_bytes(r)
(OUT/'c6-run-bindings.json').write_bytes(r_raw)
assert load_run_bindings(OUT/'c6-run-bindings.json') == r
R = commit(overlay_tree(P, R_PATH, r_raw), [P],
           'Bind all seven C6 v19 stages to real independent source identities\n\nEvidence-only child of P. Hash-bound user-interruption lineage in P; v18 has zero available sealed records and is never resumed or relabelled. All economic inputs, runtime, workflow, predicates, gates and source blobs unchanged.')
validate_implementation_git_proofs(p,r,repository=ROOT,bindings_revision=R)
merge_tree = text('merge-tree','--write-tree',PR_HEAD,S).splitlines()[0]
assert merge_tree == r['implementations']['I_S']['tree']
H = commit(merge_tree, [PR_HEAD,S],
           'Integrate frozen C6 v19 recovery identity into existing PR63\n\nNormal ancestry-preserving merge; exact independent I_S tree for engineering only, not S selection. Base source remains separate. No strategy/code/test/data/dependency/workflow change from the previous PR head. Formal acceptance and protected main merge remain pending.')
assert changed_paths(PR_HEAD,H) == [P_PATH]
identities = {'P':P,'I_B':B,'I_S':S,'R':R,'PR_head':H,'main':MAIN,
              'trees':{k:text('rev-parse',v+'^{tree}') for k,v in [('P',P),('I_B',B),('I_S',S),('R',R),('PR_head',H)]},
              'P_blob':pi['git_blob'],'P_sha256':pi['sha256'],
              'R_blob':blob_identity(R,R_PATH)['git_blob'],'R_sha256':hashlib.sha256(r_raw).hexdigest()}
# Generate the actual immutable envelope from R, not from remembered parameters.
record=r['binding_records'][0]
inputs = {'source_revision':B,'run_bindings_revision':R,'workflow_revision':r['workflow']['revision'],
          'binding_id':record['workflow_binding_id'],'candidate_id':record['candidate_id'],
          'logical_run_id':record['logical_run_id'],'attempt_id':record['initial_attempt_id'],
          'resume_from':'','resume_workflow_run_id':'','d_commit':'','d_selection_blob_oid':'',
          'd_selection_file_sha256':'','producer_identity_json':json.dumps({k:'' for k in ('artifact_full_byte_sha256','attempt_id','binding_id','logical_run_id','workflow_run_id')},sort_keys=True,separators=(',',':')),
          **{k:record['runtime'][k] for k in ('runner_image_os','runner_image_version','python_version')}}
assert set(inputs)==set(workflow['exact_dispatch_input_order'])
(OUT/'dispatch-request.json').write_bytes(canonical_json_bytes({'schema_version':1,'workflow_ref':r['workflow']['dispatch_ref'],'inputs':inputs}))
(OUT/'identities.json').write_bytes(canonical_json_bytes(identities))
proof = {'kind':'non_economic_interruption_rebinding','all_old_objects_preserved':OLD,
         'unchanged_economic_roots':[k for k in p if k not in {'experiment_id','frozen_at','authority','run_templates','workflow_trigger_matrix'}],
         'only_changed_source_path':P_PATH,'base_changed_paths_from_P':len(r['implementations']['I_B']['changed_paths']),
         'S_changed_paths_from_Base':len(r['implementations']['I_S']['changed_paths']),
         'record_signatures_verified':7,'P_R_actual_git_proofs':'passed','PR_merge_tree_equals_I_S':True,
         'local_python':sys.version,'new_candidate_CI':'not_run','economic_dispatch_performed':False,
         'identities':identities}
(OUT/'build-proof.json').write_bytes(canonical_json_bytes(proof))
assert not text('status','--porcelain','-uall')
print(json.dumps(identities,indent=2))
