"""Advance only PR63's frozen C6 v19 stages; never replay, tune, or merge a strategy.

The existing bound runner owns economics and same-logical checkpoint continuation.
This separate controller consumes complete sealed results using that runner's exact
validators, publishes R's mechanical D, and dispatches only R-declared successors.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = 'ychenracing/trade'
PR = 63
P_COMMIT = '3611ac948287ee147ed40e77eda8ea01bab27a35'
BASE = '168cd3856cc1b60c928fe54c4a826b6045df01e9'
S_SOURCE = '00485e8ceb67f4105d76fa62368f583ce82a81c5'
R_COMMIT = '4db5cadc90d450f513dfdda10e0436cf330c62f3'
R_SHA256 = '9c7fb1a92d8e04c5b95d1d18503ccb4e313f3dde6a3fa73fe6f92e24830956ac'
WORKFLOW = 'd8bc65f3edaf1869e0c6c26ba9f26d7e7931ced4'
ANCHOR = 'codex/c6-v17-workflow-anchor'
D_REF = 'codex/c6-selection-v19'
P_PATH = 'artifacts/diagnostics/c6-preregistration.json'
R_PATH = 'artifacts/diagnostics/c6-run-bindings.json'
D_PATH = 'artifacts/diagnostics/c6-selection.json'
REFS = {'codex/c6-preregistration-v19': P_COMMIT, 'codex/c6-base-v19': BASE,
        'codex/c6-s-v19': S_SOURCE, 'codex/c6-evidence-v19': R_COMMIT, ANCHOR: WORKFLOW}
INPUTS = {'source_revision', 'run_bindings_revision', 'workflow_revision', 'binding_id',
          'candidate_id', 'logical_run_id', 'attempt_id', 'resume_from', 'resume_workflow_run_id',
          'd_commit', 'd_selection_blob_oid', 'd_selection_file_sha256', 'producer_identity_json',
          'runner_image_os', 'runner_image_version', 'python_version'}
EMPTY_PRODUCER = {key: '' for key in ('artifact_full_byte_sha256', 'attempt_id', 'binding_id',
                                     'logical_run_id', 'workflow_run_id')}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                       allow_nan=False) + '\n').encode()


def predicate_failures(specs, rows):
    require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), 'malformed predicates')
    require([row.get('predicate_id') for row in rows] == [spec['id'] for spec in specs], 'predicate order/coverage')
    require(all(type(row.get('passed')) is bool for row in rows), 'predicate result must be boolean')
    return {row['predicate_id'] for row in rows if not row['passed']}


def next_step(specs, base_rows, residuals, qualification, s_rows):
    """Apply P's five branches, never a highest-return or handpicked-case rule."""
    require(isinstance(residuals, list) and all(type(x) is str for x in residuals)
            and residuals == sorted(set(residuals)), 'invalid residual list')
    require(qualification is None or type(qualification) is bool, 'invalid qualification result')
    failures = predicate_failures(specs, base_rows)
    deferred = {spec['id'] for spec in specs if spec.get('selection_defer_if_base_residual_exists') is True} if residuals else set()
    if failures - deferred:
        require(qualification is None and s_rows is None, 'correctness/undeferred failure forbids S')
        return 'BASE_REJECTED'
    if not residuals:
        require(qualification is None and s_rows is None, 'empty residual forbids S')
        return 'BASE_SELECTED'
    if qualification is None:
        require(s_rows is None, 'S requires qualification first')
        return 'QUALIFY'
    if not qualification:
        require(s_rows is None, 'failed qualification forbids S')
        return 'QUALIFICATION_REJECTED'
    if s_rows is None:
        return 'RUN_S'
    return 'BASE_PLUS_S_REJECTED' if predicate_failures(specs, s_rows) else 'BASE_PLUS_S_SELECTED'


def rejection_reasons(branch):
    reasons = {'BASE_REJECTED': 'BASE_L1_PREDICATE_FAILED',
               'QUALIFICATION_REJECTED': 'S_QUALIFICATION_FAILED',
               'BASE_PLUS_S_REJECTED': 'BASE_PLUS_S_L1_PREDICATE_FAILED'}
    return [reasons[branch]] if branch in reasons else []


def producer_identity(claim):
    if claim is None:
        return dict(EMPTY_PRODUCER)
    return {key: claim[key] for key in EMPTY_PRODUCER if key != 'binding_id'} | {'binding_id': claim.get('binding_id', claim['record_id'])}


def dispatch_request(record, r_commit, producer, decision):
    values = {key: '' for key in INPUTS}
    values.update(source_revision=record['source_revision'], run_bindings_revision=r_commit,
                  workflow_revision=record['workflow']['revision'], binding_id=record['workflow_binding_id'],
                  candidate_id=record['candidate_id'], logical_run_id=record['logical_run_id'],
                  attempt_id=record['initial_attempt_id'],
                  producer_identity_json=canonical(producer_identity(producer)).decode().rstrip('\n'))
    values.update({key: record['runtime'][key] for key in ('runner_image_os', 'runner_image_version', 'python_version')})
    if decision is not None:
        values.update(d_commit=decision['commit'], d_selection_blob_oid=decision['selection_blob_oid'],
                      d_selection_file_sha256=decision['selection_file_sha256'])
    require(all(type(v) is str and '\n' not in v and '\r' not in v for v in values.values()), 'invalid dispatch value')
    return {'ref': record['workflow']['dispatch_ref'], 'inputs': values}


def record_runs(record, history):
    prefix = f"c6-bound-{record['workflow_binding_id']}-{record['logical_run_id']}-"
    return sorted((r for r in history if str(r.get('display_title', '')).startswith(prefix)), key=lambda r: r['id'])


def initial_allowed(record, history):
    return not record_runs(record, history)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self):
        self.root = f'https://api.github.com/repos/{REPOSITORY}/'
        self.token = os.environ['GH_TOKEN']

    def request(self, path, payload=None, *, optional=False):
        require(not path.startswith('/') and '..' not in path, 'unsafe API path')
        request = urllib.request.Request(self.root + path, data=None if payload is None else canonical(payload),
                    headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                             'X-GitHub-Api-Version': '2022-11-28'}, method='GET' if payload is None else 'POST')
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if optional and exc.code == 404:
                return None
            raise
        require(len(raw) <= 16 * 1024 * 1024, 'oversized API response')
        return json.loads(raw) if raw else None

    def history(self):
        found = []
        for page in range(1, 101):
            result = self.request('actions/workflows/c6-bound-economic.yml/runs?event=workflow_dispatch'
                                  f'&per_page=100&page={page}')
            found.extend(result['workflow_runs'])
            if len(result['workflow_runs']) < 100:
                return found
        raise ValueError('incomplete workflow history')

    def live_guard(self):
        pr = self.request(f'pulls/{PR}')
        if pr['state'] != 'open' or pr['merged']:
            return False
        require(pr['head']['ref'] == 'codex/c6-causal-risk-closure-v11'
                and pr['head']['repo']['full_name'] == REPOSITORY, 'task PR identity changed')
        # A user-requested stop remains effective. Never enable paused workflows.
        for filename in ('c6-bound-economic.yml', 'c6-auto-resume.yml', 'c6-dispatch.yml'):
            if self.request('actions/workflows/' + filename)['state'] != 'active':
                return False
        for ref, expected in REFS.items():
            require(self.request('git/ref/heads/' + ref)['object']['sha'] == expected, 'frozen ref moved: ' + ref)
        return True


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root).decode().strip()


def load_frozen(source, bindings_root):
    """Verify exact checkouts before adding anything to the import search path."""
    require(git(source, 'rev-parse', 'HEAD') == BASE, 'controller verifier checkout is not frozen Base')
    require(git(bindings_root, 'rev-parse', 'HEAD') == R_COMMIT, 'wrong R checkout')
    for root in (source, bindings_root):
        require(not git(root, 'status', '--porcelain', '-uall'), 'dirty frozen checkout')
    raw = (bindings_root / R_PATH).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == R_SHA256, 'R bytes differ')
    r = json.loads(raw)
    require(r['P']['commit'] == P_COMMIT and r['implementations']['I_B']['commit'] == BASE
            and r['implementations']['I_S']['commit'] == S_SOURCE, 'source aliases differ')
    for path, identity in r['implementations']['I_B']['required_blobs'].items():
        require(git(source, 'rev-parse', 'HEAD:' + path) == identity['git_blob']
                and hashlib.sha256((source / path).read_bytes()).hexdigest() == identity['sha256'], 'source blob differs: ' + path)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    from quantfusion.application import c6_contract as contract
    from quantfusion.application import c6_bound_run as bound
    from quantfusion.application import c6_predicates as predicates
    from quantfusion.io import c6_stream as stream
    p = contract.load_preregistration(source / P_PATH, repository=source)
    r = contract.load_run_bindings(bindings_root / R_PATH)
    contract.validate_implementation_git_proofs(p, r, repository=source, bindings_revision=R_COMMIT)
    return contract, bound, predicates, stream, p, r


class Relay:
    def __init__(self, api, source, bindings_root, output):
        self.api, self.source, self.output = api, source, output
        self.contract, self.bound, self.predicates, self.stream, self.p, self.r = load_frozen(source, bindings_root)
        self.records = {r['record_id']: r for r in self.r['binding_records']}
        self.r_identity = {'commit': R_COMMIT, 'tree': git(bindings_root, 'rev-parse', 'HEAD^{tree}'),
                           'blob': git(bindings_root, 'rev-parse', 'HEAD:' + R_PATH), 'sha256': R_SHA256}
        self.store = self.bound.GitHubActionsLeaseStore(REPOSITORY, api.token)
        self.exports = []
        self.cache = {}

    def read_result(self, record_id, history, decision=None):
        """Authenticate a final export, then reuse native schema/formula/hash validation."""
        if record_id in self.cache:
            return self.cache[record_id]
        record = self.records[record_id]
        runs = record_runs(record, history)
        require(runs, 'required producer has not run: ' + record_id)
        run = runs[-1]
        require(run['status'] == 'completed', 'producer is active')
        require(run['conclusion'] == 'success' or (record['stage'] == 'L4' and run['conclusion'] == 'failure'), 'producer failed or cancelled')
        require(run['path'] == '.github/workflows/c6-bound-economic.yml' and run['workflow_id'] == 349948458
                and run['head_branch'] == ANCHOR and run['head_sha'] == WORKFLOW
                and run['event'] == 'workflow_dispatch' and run['run_attempt'] == 1, 'producer workflow identity')
        artifacts = self.store._pages(f"actions/runs/{run['id']}/artifacts", 'artifacts')
        require(len(artifacts) == 1, 'missing/ambiguous producer artifact')
        export = self.store._export(run['id'], artifacts[0])
        self.exports.append(export)
        m = export.manifest
        expected = {'repository': REPOSITORY, 'workflow_run_id': str(run['id']), 'workflow_run_attempt': '1',
                    'binding_id': record['workflow_binding_id'], 'logical_run_id': record['logical_run_id'],
                    'source_revision': record['source_revision'], 'candidate_id': record['candidate_id'],
                    'run_bindings_revision': R_COMMIT, 'workflow_revision': WORKFLOW,
                    **{k: record['runtime'][k] for k in ('runner_image_os', 'runner_image_version', 'python_version')}}
        require(all(m[k] == v for k, v in expected.items()), 'producer manifest differs from R')
        require(run['display_title'] == f"c6-bound-{m['binding_id']}-{m['logical_run_id']}-{m['attempt_id']}"
                and artifacts[0]['name'] == f"c6-bound-{m['logical_run_id']}-{m['attempt_id']}", 'producer name/attempt mismatch')
        if m['kind'] == 'checkpoint':
            return None
        require(m['kind'] == 'result', 'unsupported export kind')
        payload_names = set(export.files) - {'digest.json'}
        require(len(payload_names) == 1 and len(export.files) == 2, 'result file set differs')
        raw = export.files[payload_names.pop()]
        payload = self.stream.load_object(raw)
        self.bound.validate_result_payload(payload, record, self.p)
        require((record['stage'] in {'L2', 'L4'}) == (decision is not None), 'stage/decision presence differs')
        d_identity, implementation = (None, None) if decision is None else decision
        for key, value in (('d_commit', '' if d_identity is None else d_identity['commit']),
                           ('d_selection_blob_oid', '' if d_identity is None else d_identity['selection_blob_oid']),
                           ('d_selection_file_sha256', '' if d_identity is None else d_identity['selection_file_sha256'])):
            require(m[key] == value, 'result D identity differs')
        ids = ([f'qualification/{x}' for x in payload['residual_ids']] if record['stage'] == 'QUALIFICATION'
               else self.bound.execution_item_ids(record, self.p))
        signature = self.bound.runtime_binding_signature({
            'record_signature': record['record_signature'], 'run_bindings_revision': R_COMMIT,
            **{k: m[k] for k in ('attempt_id', 'workflow_run_id', 'resume_from', 'resume_workflow_run_id', 'fencing_token_sha256')},
            'direct_producer_identity': m['producer_identity'],
            'transitive_base_producer_identity': payload['base_producer_identity'] if record_id == 'c6.base_plus_s.l1' else EMPTY_PRODUCER,
            'D': d_identity, 'C': implementation, 'selection_status': 'unselected' if decision is None else 'selected',
            'item_manifest_count': len(ids), 'item_manifest_sha256': hashlib.sha256(''.join(x+'\n' for x in ids).encode()).hexdigest()})
        digest = self.stream.load_object(export.files['digest.json'])
        expected_digest = self.bound.build_digest(stage=record['stage'], record_id=record_id, binding_signature=signature,
            p_identity=self.r['P'], r_revision=R_COMMIT, source_revision=record['source_revision'], source_tree=record['source_tree'],
            d_identity=d_identity, implementation=implementation,
            artifact_path=self.bound.resolve_attempt_paths(record, m['attempt_id'])['payload'].as_posix(),
            artifact_bytes=raw, payload_schema=record['canonical_payload_schema'],
            payload=self.bound.result_payload(payload, record['stage'], self.p), exit_code=digest['exit_code'])
        require(digest == expected_digest, 'result digest/signature differs')
        if record['stage'] != 'L4':
            require(digest['exit_code'] == 0 and payload['complete'] is True, 'producer is not complete')
        else:
            require((digest['exit_code'], payload['acceptance_status'], payload['canonical']) in
                    ((0, 'accepted', True), (2, 'rejected', False)), 'official result status/exit mismatch')
        claim = {k: digest[k] for k in ('record_id', 'artifact_path', 'artifact_byte_size',
                 'artifact_full_byte_sha256', 'canonical_result_payload_sha256')}
        claim.update({k: m[k] for k in ('candidate_id', 'workflow_run_id', 'logical_run_id', 'attempt_id')})
        claim['manifest_full_byte_sha256'] = hashlib.sha256(export.manifest_bytes).hexdigest()
        result = (payload, claim, export)
        self.cache[record_id] = result
        return result

    def dispatch(self, record_id, producer=None, decision=None):
        record = self.records[record_id]
        require(self.api.live_guard(), 'task paused or closed before dispatch')
        require(initial_allowed(record, self.api.history()), 'existing run forbids new initial dispatch')
        require(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                <= record['attempt_policy']['dispatch_deadline_utc'], 'dispatch deadline expired')
        if producer is not None:
            producer = dict(producer, binding_id=self.records[producer['record_id']]['workflow_binding_id'])
        request = dispatch_request(record, R_COMMIT, producer, decision)
        envelope = {'schema_version': 1, 'workflow_ref': ANCHOR, 'inputs': request['inputs']}
        # The existing immutable envelope branch is also the unique dispatch claim.
        # If a write/HTTP outcome is ambiguous, it is retained; never blindly retry.
        ref = f"codex/c6-dispatch/{record['logical_run_id']}/{record['workflow_binding_id']}/a0"
        require(self.api.request('git/ref/heads/' + ref, optional=True) is None,
                'dispatch envelope already exists; reconcile its outcome, do not duplicate')
        tree = self.api.request('git/trees', {'base_tree': self.api.request('git/commits/' + WORKFLOW)['tree']['sha'],
            'tree': [{'path': '.github/c6-dispatch-request.json', 'mode': '100644', 'type': 'blob', 'content': canonical(envelope).decode()}]})
        commit = self.api.request('git/commits', {'tree': tree['sha'], 'parents': [WORKFLOW],
            'message': f"Dispatch R-bound {record_id} after authenticated C6 v19 predecessor\n\nNo candidate/input changes. R={R_COMMIT}. Exact P/R mechanical transition; native reruns forbidden."})
        self.api.request('git/refs', {'ref': 'refs/heads/' + ref, 'sha': commit['sha']})
        require(self.api.request('git/ref/heads/' + ref)['object']['sha'] == commit['sha'], 'envelope read-back mismatch')
        require(self.api.live_guard() and initial_allowed(record, self.api.history()), 'concurrent task/dispatch change')
        # GITHUB_TOKEN-created refs do not trigger push workflows. Explicit native
        # workflow_dispatch is required, while the exact request envelope is retained.
        self.api.request('actions/workflows/c6-bound-economic.yml/dispatches', request)
        return {'status': 'dispatched', 'record_id': record_id, 'envelope_commit': commit['sha'], 'logical_run_id': record['logical_run_id']}

    def publish_decision(self, selection, base, qualification, selected_s):
        c = self.contract
        defs = self.p['schema_catalog']['definitions']
        self.bound.validate_wire_value(selection, defs['D_selection']['wire_schema'], defs)
        c.validate_selection_shape(selection, self.p)
        scenario_ids = (self.source / self.p['scenario_manifests']['L1_ECONOMIC_SCENARIO_IDS']['path']).read_text().splitlines()
        c.validate_selection_producer_payloads(selection, base, qualification, selected_s, scenario_ids)
        if qualification is not None:
            self.predicates.validate_qualification_results(qualification, base)
        if selected_s is not None:
            self.predicates.validate_s_comparison_results(selected_s, base, scenario_ids)
        raw = c.canonical_json_bytes(selection)
        # Write only outside the source checkout; existing native D validation reads P there.
        path = self.output / 'c6-selection.json'
        path.write_bytes(raw)
        if selection['status'] == 'selected':
            c.validate_selection_commit(path, run_bindings=self.r, run_bindings_revision=R_COMMIT,
                                        candidate_id=selection['selected_candidate'])
        old = self.api.request('git/ref/heads/' + D_REF, optional=True)
        tree = self.api.request('git/trees', {'base_tree': self.r_identity['tree'], 'tree': [
            {'path': D_PATH, 'mode': '100644', 'type': 'blob', 'content': raw.decode()}]})
        if old is not None:
            sha = old['object']['sha']
            existing = self.api.request('git/commits/' + sha)
            require([x['sha'] for x in existing['parents']] == [R_COMMIT] and existing['tree']['sha'] == tree['sha'], 'existing D differs')
        else:
            require(self.api.live_guard(), 'task paused/closed before D publication')
            sha = self.api.request('git/commits', {'tree': tree['sha'], 'parents': [R_COMMIT],
                  'message': 'Seal mechanical C6 v19 selection: ' + selection['branch']})['sha']
            self.api.request('git/refs', {'ref': 'refs/heads/' + D_REF, 'sha': sha})
        require(self.api.request('git/ref/heads/' + D_REF)['object']['sha'] == sha, 'D ref read-back differs')
        content = self.api.request('contents/' + D_PATH + '?ref=' + sha)
        require(base64.b64decode(content['content']) == raw, 'D bytes read-back differs')
        return {'commit': sha, 'selection_blob_oid': content['sha'], 'selection_file_sha256': hashlib.sha256(raw).hexdigest()}

    def advance(self, history):
        base_result = self.read_result('c6.base.l1', history)
        if base_result is None:
            return {'status': 'checkpoint_owned_by_existing_resume'}
        base, base_claim, _ = base_result
        specs = self.p['diagnostic_predicate_manifests']['L1_APPLICABLE_DIAGNOSTIC_PREDICATES']
        mdd = next(row for row in base['diagnostic_predicates'] if row['predicate_id'] == 'l1.mdd.noncanonical_18pct_screen')
        failed_ids = mdd['observed']['failed_item_ids']
        require(all(x.startswith('C6-Base::') for x in failed_ids), 'non-Base residual')
        residuals = [x.removeprefix('C6-Base::') for x in failed_ids]
        q = s = q_claim = s_claim = None
        step = next_step(specs, base['diagnostic_predicates'], residuals, None, None)
        if step == 'QUALIFY':
            if initial_allowed(self.records['c6.s.qualification'], history):
                return self.dispatch('c6.s.qualification', base_claim)
            q_result = self.read_result('c6.s.qualification', history)
            if q_result is None:
                return {'status': 'checkpoint_owned_by_existing_resume'}
            q, q_claim, _ = q_result
            require(q['base_producer_identity'] == producer_identity(base_claim) and q['residual_ids'] == residuals,
                    'qualification producer/residual mismatch')
            self.predicates.validate_qualification_results(q, base)
            step = next_step(specs, base['diagnostic_predicates'], residuals, q['all_passed'], None)
        if step == 'RUN_S':
            if initial_allowed(self.records['c6.base_plus_s.l1'], history):
                return self.dispatch('c6.base_plus_s.l1', q_claim)
            s_result = self.read_result('c6.base_plus_s.l1', history)
            if s_result is None:
                return {'status': 'checkpoint_owned_by_existing_resume'}
            s, s_claim, _ = s_result
            step = next_step(specs, base['diagnostic_predicates'], residuals, True, s['diagnostic_predicates'])
        chosen = 'C6-Base' if step == 'BASE_SELECTED' else ('C6-Base+S' if step == 'BASE_PLUS_S_SELECTED' else None)
        implementation = None if chosen is None else self.r['implementations']['I_B' if chosen == 'C6-Base' else 'I_S']
        selection = {'schema_version': 2, 'kind': 'c6_selection', 'branch': step,
                     'status': 'selected' if chosen else 'rejected', 'P': self.r['P'], 'R': self.r_identity,
                     'base_l1': base_claim, 'base_l1_predicates': base['diagnostic_predicates'], 'residual_ids': residuals,
                     'residual_ids_sha256': hashlib.sha256(''.join(x+'\n' for x in residuals).encode()).hexdigest(),
                     's_qualification': q_claim, 'base_plus_s_l1': s_claim,
                     'base_plus_s_l1_predicates': None if s is None else s['diagnostic_predicates'],
                     'selected_candidate': chosen, 'C': implementation, 'rejection_reasons': rejection_reasons(step)}
        decision = self.publish_decision(selection, base, q, s)
        if chosen is None:
            return {'status': 'economic_rejection', 'D': decision, 'branch': step, 'merge_authorized': False}
        prefix = 'c6.base' if chosen == 'C6-Base' else 'c6.base_plus_s'
        l2_id, l4_id = prefix + '.selected.l2', prefix + '.selected.l4'
        if initial_allowed(self.records[l2_id], history):
            return self.dispatch(l2_id, decision=decision)
        l2_result = self.read_result(l2_id, history, (decision, implementation))
        if l2_result is None:
            return {'status': 'checkpoint_owned_by_existing_resume'}
        l2, l2_claim, l2_export = l2_result
        l2_specs = self.p['diagnostic_predicate_manifests']['L2_APPLICABLE_DIAGNOSTIC_PREDICATES']
        if predicate_failures(l2_specs, l2['diagnostic_predicates']):
            return {'status': 'economic_rejection', 'stage': 'L2', 'D': decision, 'merge_authorized': False}
        self.bound.validate_l2_gate(l2_export, decision, implementation, self.p)
        if initial_allowed(self.records[l4_id], history):
            return self.dispatch(l4_id, l2_claim, decision)
        formal_result = self.read_result(l4_id, history, (decision, implementation))
        if formal_result is None:
            return {'status': 'checkpoint_owned_by_existing_resume'}
        formal, formal_claim, _ = formal_result
        return {'status': 'formal_accepted_requires_release_review' if formal['canonical'] else 'economic_rejection',
                'stage': 'L4', 'D': decision, 'C': implementation, 'formal': formal_claim,
                'acceptance_status': formal['acceptance_status'], 'canonical': formal['canonical'], 'merged': False}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--bindings', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY and os.environ.get('GITHUB_RUN_ATTEMPT') == '1', 'untrusted invocation or native rerun')
    require(os.environ.get('GITHUB_REF_NAME') == 'main', 'only protected main can advance stages')
    api = API()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if not api.live_guard():
        ready, state = False, {'status': 'task_closed_or_paused'}
    else:
        history = api.history()
        current = [run for run in history if '-c6-v19-' in run.get('display_title', '')]
        active = [run for run in current if run['status'] != 'completed']
        ready = bool(current) and not active
        state = {'status': 'active' if active else ('inspect_sealed_result' if current else 'not_started'),
                 'active_run_ids': [run['id'] for run in active]}
    if args.probe:
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.write('ready=' + str(ready).lower() + '\n')
    elif ready:
        require(args.source is not None and args.bindings is not None, 'missing frozen checkouts')
        source, bindings = args.source.resolve(), args.bindings.resolve()
        os.chdir(source)
        relay = Relay(api, source, bindings, args.output.resolve())
        try:
            state = relay.advance(history)
        finally:
            for export in relay.exports:
                export.close()
    (args.output / 'stage-status.json').write_bytes(canonical(state))
    if not args.probe:
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            terminal = state['status'] in {'economic_rejection', 'formal_accepted_requires_release_review'}
            output.write('terminal=' + str(terminal).lower() + '\n')
    print(json.dumps(state, sort_keys=True))


if __name__ == '__main__':
    main()
