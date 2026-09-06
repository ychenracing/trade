"""Synthetic tests for the C6 stage relay; never load market data or run a replay."""
import copy
import importlib.util
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('relay', HERE / 'c6_stage_advance.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.specs = [
            {'id': 'identity'},
            {'id': 'mdd', 'selection_defer_if_base_residual_exists': True},
            {'id': 'retention', 'selection_defer_if_base_residual_exists': True},
            {'id': 'correctness'},
        ]
        self.rows = [{'predicate_id': s['id'], 'passed': True} for s in self.specs]

    def plan(self, residuals=None, qualification=None, s_rows=None, rows=None):
        return relay.next_step(self.specs, self.rows if rows is None else rows,
                               [] if residuals is None else residuals, qualification, s_rows)

    def test_no_residual_selects_only_base(self):
        self.assertEqual(self.plan(), 'BASE_SELECTED')
        with self.assertRaises(ValueError):
            self.plan(qualification=True)

    def test_retention_failure_does_not_skip_eligible_s(self):
        self.rows[1]['passed'] = self.rows[2]['passed'] = False
        self.assertEqual(self.plan(['synthetic']), 'QUALIFY')
        self.assertEqual(self.plan(['synthetic'], False), 'QUALIFICATION_REJECTED')
        self.assertEqual(self.plan(['synthetic'], True), 'RUN_S')
        good = [dict(x, passed=True) for x in self.rows]
        self.assertEqual(self.plan(['synthetic'], True, good), 'BASE_PLUS_S_SELECTED')
        good[-1]['passed'] = False
        self.assertEqual(self.plan(['synthetic'], True, good), 'BASE_PLUS_S_REJECTED')

    def test_correctness_failure_cannot_be_rescued_by_s(self):
        self.rows[3]['passed'] = False
        self.assertEqual(self.plan(['synthetic']), 'BASE_REJECTED')
        with self.assertRaises(ValueError):
            self.plan(['synthetic'], True)

    def test_no_residual_retention_failure_is_rejected(self):
        self.rows[2]['passed'] = False
        self.assertEqual(self.plan(), 'BASE_REJECTED')

    def test_malformed_predicates_and_ineligible_s_fail_closed(self):
        for rows in (self.rows[:-1], list(reversed(self.rows)), self.rows + self.rows[:1],
                     [dict(x, passed=1) for x in self.rows]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.plan(rows=rows)
        with self.assertRaises(ValueError):
            self.plan(['x'], False, self.rows)
        with self.assertRaises(ValueError):
            self.plan(['x'], None, self.rows)
        with self.assertRaises(ValueError):
            self.plan(['x'], 'true')
        with self.assertRaises(ValueError):
            self.plan(['x', 'x'])


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.record = {'workflow_binding_id': 'c6.s.qualification', 'logical_run_id': 'c6-v19-s-qualification',
                       'candidate_id': 'C6-Base+S', 'initial_attempt_id': 'a0',
                       'source_revision': '1' * 40,
                       'workflow': {'revision': '2' * 40, 'dispatch_ref': 'frozen'},
                       'runtime': {'runner_image_os': 'ubuntu24', 'runner_image_version': 'image', 'python_version': '3.12.14'}}
        self.claim = {'record_id': 'c6.base.l1', 'workflow_run_id': '42', 'attempt_id': 'a0',
                      'logical_run_id': 'c6-v19-base-l1', 'artifact_full_byte_sha256': '3' * 64}

    def test_exact_initial_dispatch_fields_and_producer(self):
        request = relay.dispatch_request(self.record, '4' * 40, self.claim, None)
        self.assertEqual(request['ref'], 'frozen')
        self.assertEqual(set(request['inputs']), relay.INPUTS)
        self.assertTrue(all(isinstance(v, str) for v in request['inputs'].values()))
        self.assertEqual(request['inputs']['attempt_id'], 'a0')
        self.assertEqual(request['inputs']['resume_from'], '')
        self.assertEqual(request['inputs']['resume_workflow_run_id'], '')
        self.assertEqual(json.loads(request['inputs']['producer_identity_json'])['binding_id'], 'c6.base.l1')

    def test_l4_uses_l2_workflow_binding_not_record_id(self):
        claim = dict(self.claim, record_id='c6.base.selected.l2', binding_id='c6.selected.l2')
        request = relay.dispatch_request(self.record, '4' * 40, claim, None)
        self.assertEqual(json.loads(request['inputs']['producer_identity_json'])['binding_id'], 'c6.selected.l2')

    def test_d_triple_is_transport_only(self):
        d = {'commit': '5' * 40, 'selection_blob_oid': '6' * 40, 'selection_file_sha256': '7' * 64}
        result = relay.dispatch_request(self.record, '4' * 40, None, d)['inputs']
        self.assertEqual(result['d_commit'], d['commit'])
        self.assertEqual(result['d_selection_blob_oid'], d['selection_blob_oid'])
        self.assertTrue(all(v == '' for v in json.loads(result['producer_identity_json']).values()))
        changed = copy.deepcopy(d)
        changed['commit'] = '8' * 40
        other = relay.dispatch_request(self.record, '4' * 40, None, changed)['inputs']
        self.assertEqual([key for key in result if result[key] != other[key]], ['d_commit'])

    def test_any_existing_attempt_blocks_initial_dispatch(self):
        prefix = 'c6-bound-c6.s.qualification-c6-v19-s-qualification-'
        for status in ('queued', 'in_progress', 'completed', 'waiting'):
            for suffix in ('a0', 'r1-' + 'a' * 12):
                rows = [{'id': 1, 'display_title': prefix + suffix, 'status': status}]
                self.assertFalse(relay.initial_allowed(self.record, rows))
        self.assertTrue(relay.initial_allowed(self.record, [{'display_title': 'unrelated'}]))


class FrozenValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if len(sys.argv) < 2 or not Path(sys.argv[1]).is_dir():
            raise unittest.SkipTest('frozen source not supplied')
        root = Path(sys.argv[1]).resolve()
        sys.path.insert(0, str(root))
        from quantfusion.application import c6_contract
        cls.contract = c6_contract
        cls.p = json.loads((root / 'artifacts/diagnostics/c6-preregistration.json').read_text())

    def test_every_completed_branch_is_accepted_by_frozen_shape_validator(self):
        specs = self.p['diagnostic_predicate_manifests']['L1_APPLICABLE_DIAGNOSTIC_PREDICATES']
        all_true = [{'predicate_id': s['id'], 'passed': True} for s in specs]
        for branch in ('BASE_REJECTED', 'BASE_SELECTED', 'QUALIFICATION_REJECTED', 'BASE_PLUS_S_REJECTED', 'BASE_PLUS_S_SELECTED'):
            base = copy.deepcopy(all_true)
            residuals = [] if branch == 'BASE_SELECTED' else ['synthetic']
            if residuals:
                next(r for r in base if r['predicate_id'] == 'l1.mdd.noncanonical_18pct_screen')['passed'] = False
            if branch == 'BASE_REJECTED':
                base[0]['passed'] = False
            q = branch not in ('BASE_REJECTED', 'BASE_SELECTED')
            s = branch.startswith('BASE_PLUS_S_')
            sr = copy.deepcopy(all_true) if s else None
            if branch == 'BASE_PLUS_S_REJECTED':
                sr[-1]['passed'] = False
            self.assertEqual(relay.next_step(specs, base, residuals, (s if q else None), sr), branch)
            chosen = 'C6-Base' if branch == 'BASE_SELECTED' else ('C6-Base+S' if branch == 'BASE_PLUS_S_SELECTED' else None)
            selection = {'branch': branch, 'base_l1_predicates': base, 'residual_ids': residuals,
                         's_qualification': {} if q else None, 'base_plus_s_l1': {} if s else None,
                         'base_plus_s_l1_predicates': sr, 'status': 'selected' if chosen else 'rejected',
                         'selected_candidate': chosen, 'C': {} if chosen else None,
                         'rejection_reasons': relay.rejection_reasons(branch)}
            with self.subTest(branch=branch):
                self.contract.validate_selection_shape(selection, self.p)


class NativeExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if len(sys.argv) < 2 or not Path(sys.argv[1]).is_dir():
            raise unittest.SkipTest('frozen source not supplied')
        sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
        from quantfusion.application import c6_bound_run as bound
        from quantfusion.application import c6_contract as contract
        from quantfusion.io import c6_stream as stream
        cls.bound, cls.contract, cls.stream = bound, contract, stream

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.record = {'record_id': 'synthetic.selected.l2', 'workflow_binding_id': 'c6.selected.l2',
                       'logical_run_id': 'c6-v19-synthetic-l2', 'candidate_id': 'C6-Base', 'stage': 'L2',
                       'source_revision': relay.BASE, 'source_tree': '1' * 40, 'record_signature': '2' * 64,
                       'runtime': {'runner_image_os': 'ubuntu24', 'runner_image_version': 'synthetic', 'python_version': '3.12.14'},
                       'canonical_payload_schema': {'name': 'fixture', 'version': 2},
                       'paths': {'attempt_root_template': 'artifacts/checkpoints/c6/{logical_run_id}/attempts/{attempt_id}',
                                 'payload_path_template': '{attempt_root}/payload.json.gz',
                                 'digest_path_template': '{attempt_root}/digest.json',
                                 'manifest_path_template': '{attempt_root}/manifest.json',
                                 'checkpoint_wrapper_path_template': '{attempt_root}/checkpoint.json',
                                 'child_checkpoint_path_template': '{attempt_root}/child-checkpoint.bin.gz'}}
        self.p = {'schema_catalog': {'schema_version': 2, 'definitions': {'fixture': {'wire_schema': {
                      'type': 'object', 'properties': {'kind': {'const': 'synthetic-stage-transport'}, 'complete': {'const': True}},
                      'required': ['kind', 'complete'], 'additionalProperties': False}}}},
                  'scenario_manifests': {'L2_EXACT_SCENARIO_IDS': {'ids': ['synthetic']}}}
        self.pid = {'commit': '3' * 40, 'tree': '4' * 40, 'blob': '5' * 40, 'sha256': '6' * 64}
        self.decision = {'commit': '7' * 40, 'selection_blob_oid': '8' * 40, 'selection_file_sha256': '9' * 64}
        self.implementation = {'commit': relay.BASE, 'tree': '1' * 40}
        self.manifest = {'kind': 'result', 'repository': relay.REPOSITORY, 'workflow_run_id': '42', 'workflow_run_attempt': '1',
                         'binding_id': 'c6.selected.l2', 'logical_run_id': self.record['logical_run_id'], 'attempt_id': 'a0',
                         'source_revision': relay.BASE, 'candidate_id': 'C6-Base', 'run_bindings_revision': relay.R_COMMIT,
                         'workflow_revision': relay.WORKFLOW, **self.record['runtime'], 'resume_from': '', 'resume_workflow_run_id': '',
                         'fencing_token_sha256': 'a' * 64, 'producer_identity': relay.EMPTY_PRODUCER,
                         'd_commit': self.decision['commit'], 'd_selection_blob_oid': self.decision['selection_blob_oid'],
                         'd_selection_file_sha256': self.decision['selection_file_sha256']}
        self.payload = self.root / 'payload.json.gz'
        self.stream.write_json(self.payload, {'kind': 'synthetic-stage-transport', 'complete': True})
        ids = self.bound.execution_item_ids(self.record, self.p)
        signature = self.bound.runtime_binding_signature({'record_signature': self.record['record_signature'],
            'run_bindings_revision': relay.R_COMMIT, **{k: self.manifest[k] for k in
                ('attempt_id', 'workflow_run_id', 'resume_from', 'resume_workflow_run_id', 'fencing_token_sha256')},
            'direct_producer_identity': relay.EMPTY_PRODUCER, 'transitive_base_producer_identity': relay.EMPTY_PRODUCER,
            'D': self.decision, 'C': self.implementation, 'selection_status': 'selected', 'item_manifest_count': len(ids),
            'item_manifest_sha256': relay.hashlib.sha256(''.join(x + '\n' for x in ids).encode()).hexdigest()})
        self.digest = self.bound.build_digest(stage='L2', record_id=self.record['record_id'], binding_signature=signature,
            p_identity=self.pid, r_revision=relay.R_COMMIT, source_revision=relay.BASE, source_tree='1' * 40,
            d_identity=self.decision, implementation=self.implementation,
            artifact_path=self.bound.resolve_attempt_paths(self.record, 'a0')['payload'].as_posix(), artifact_bytes=self.payload,
            payload_schema=self.record['canonical_payload_schema'], payload=self.stream.load_object(self.payload), exit_code=0)
        self.digest_path = self.root / 'digest.json'
        self.digest_path.write_bytes(self.contract.canonical_json_bytes(self.digest))
        self.run = {'id': 42, 'status': 'completed', 'conclusion': 'success', 'run_attempt': 1,
                    'event': 'workflow_dispatch', 'path': '.github/workflows/c6-bound-economic.yml', 'workflow_id': 349948458,
                    'head_branch': relay.ANCHOR, 'head_sha': relay.WORKFLOW,
                    'display_title': 'c6-bound-c6.selected.l2-c6-v19-synthetic-l2-a0'}
        self.instance = relay.Relay.__new__(relay.Relay)
        self.instance.records = {self.record['record_id']: self.record}
        self.instance.p, self.instance.r = self.p, {'P': self.pid}
        self.instance.bound, self.instance.stream = self.bound, self.stream
        self.instance.exports, self.instance.cache = [], {}
        self.instance.store = SimpleNamespace(
            _pages=lambda *a: [{'name': 'c6-bound-c6-v19-synthetic-l2-a0'}],
            _export=lambda *a: self.bound.RemoteExport(42, self.manifest, self.contract.canonical_json_bytes(self.manifest),
                                                       {'payload.json.gz': self.payload, 'digest.json': self.digest_path}))

    def read(self):
        return self.instance.read_result(self.record['record_id'], [self.run], (self.decision, self.implementation))

    def test_actual_native_digest_signature_schema_and_compressed_transport(self):
        payload, claim, _ = self.read()
        self.assertEqual(payload['kind'], 'synthetic-stage-transport')
        self.assertEqual(claim['artifact_full_byte_sha256'], self.digest['artifact_full_byte_sha256'])
        self.assertEqual(claim['record_id'], 'synthetic.selected.l2')

    def test_checkpoint_does_not_advance_even_when_workflow_succeeded(self):
        self.manifest['kind'] = 'checkpoint'
        self.assertIsNone(self.read())

    def test_false_source_digest_or_wire_schema_is_rejected(self):
        for mutation in ('source', 'digest', 'schema', 'attempt', 'decision'):
            with self.subTest(mutation=mutation):
                self.instance.cache.clear()
                original_manifest = copy.deepcopy(self.manifest)
                original_digest = self.digest_path.read_bytes()
                original_payload = self.payload.read_bytes()
                if mutation == 'source':
                    self.manifest['source_revision'] = '0' * 40
                if mutation == 'digest':
                    self.digest_path.write_bytes(self.contract.canonical_json_bytes(dict(self.digest, artifact_byte_size=0)))
                if mutation == 'schema':
                    self.payload.write_bytes(__import__('gzip').compress(self.contract.canonical_json_bytes({'kind': 'synthetic-stage-transport', 'complete': False})))
                if mutation == 'attempt':
                    self.run['run_attempt'] = 2
                if mutation == 'decision':
                    self.manifest['d_commit'] = '0' * 40
                with self.assertRaises((ValueError, RuntimeError)):
                    self.read()
                self.manifest = original_manifest
                self.digest_path.write_bytes(original_digest)
                self.payload.write_bytes(original_payload)
                self.run['run_attempt'] = 1


class APISafetyTests(unittest.TestCase):
    def guard(self, *, closed=False, paused=False, moved=False):
        class Fake(relay.API):
            def __init__(self):
                self.reads = []

            def request(self, path, payload=None, **kwargs):
                self.reads.append(path)
                if payload is not None:
                    raise AssertionError('read-only guard attempted a write')
                if path == 'pulls/63':
                    return {'state': 'closed' if closed else 'open', 'merged': False,
                            'head': {'ref': 'codex/c6-causal-risk-closure-v11',
                                     'repo': {'full_name': relay.REPOSITORY}}}
                if path.startswith('actions/workflows/'):
                    return {'state': 'disabled_manually' if paused else 'active'}
                ref = path.removeprefix('git/ref/heads/')
                return {'object': {'sha': '0' * 40 if moved else relay.REFS[ref]}}
        return Fake()

    def test_pause_and_closed_pr_never_reenable_or_dispatch(self):
        for kwargs in ({'closed': True}, {'paused': True}):
            api = self.guard(**kwargs)
            self.assertFalse(api.live_guard())
            self.assertFalse(any('git/' in path for path in api.reads))

    def test_all_frozen_refs_are_checked_and_drift_rejected(self):
        api = self.guard()
        self.assertTrue(api.live_guard())
        self.assertEqual(len([p for p in api.reads if p.startswith('git/')]), len(relay.REFS))
        with self.assertRaises(ValueError):
            self.guard(moved=True).live_guard()

    def make_dispatcher(self, *, ambiguous=False):
        class Fake:
            def __init__(self):
                self.branch = None
                self.writes = []

            def live_guard(self):
                return True

            def history(self):
                return []

            def request(self, path, payload=None, **kwargs):
                if payload is None:
                    if path.startswith('git/ref/heads/'):
                        return None if self.branch is None else {'object': {'sha': self.branch}}
                    if path.startswith('git/commits/'):
                        return {'tree': {'sha': 'a' * 40}}
                    raise AssertionError(path)
                self.writes.append((path, payload))
                if path == 'git/trees':
                    return {'sha': 'b' * 40}
                if path == 'git/commits':
                    return {'sha': 'c' * 40}
                if path == 'git/refs':
                    self.branch = payload['sha']
                    return {'ref': payload['ref']}
                if path.endswith('/dispatches'):
                    if ambiguous:
                        raise TimeoutError('synthetic ambiguous HTTP outcome')
                    return None
                raise AssertionError(path)
        obj = relay.Relay.__new__(relay.Relay)
        obj.api = Fake()
        obj.records = {'c6.base.selected.l4': {
            'workflow_binding_id': 'c6.selected.l4', 'logical_run_id': 'c6-v19-base-l4',
            'candidate_id': 'C6-Base', 'initial_attempt_id': 'a0', 'source_revision': relay.BASE,
            'workflow': {'revision': relay.WORKFLOW, 'dispatch_ref': relay.ANCHOR},
            'attempt_policy': {'dispatch_deadline_utc': '9999-01-01T00:00:00Z'},
            'runtime': {'runner_image_os': 'ubuntu24', 'runner_image_version': 'synthetic', 'python_version': '3.12.14'}},
            'c6.base.selected.l2': {'workflow_binding_id': 'c6.selected.l2'}}
        producer = {'record_id': 'c6.base.selected.l2', 'artifact_full_byte_sha256': '1' * 64,
                    'attempt_id': 'a0', 'workflow_run_id': '42', 'logical_run_id': 'c6-v19-base-l2'}
        decision = {'commit': '2' * 40, 'selection_blob_oid': '3' * 40, 'selection_file_sha256': '4' * 64}
        return obj, producer, decision

    def test_dispatch_envelope_parent_exact_request_and_single_dispatch(self):
        obj, producer, decision = self.make_dispatcher()
        result = obj.dispatch('c6.base.selected.l4', producer, decision)
        self.assertEqual(result['status'], 'dispatched')
        calls = dict(obj.api.writes)
        self.assertEqual(calls['git/commits']['parents'], [relay.WORKFLOW])
        entries = calls['git/trees']['tree']
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['path'], entries[0]['mode']), ('.github/c6-dispatch-request.json', '100644'))
        envelope = json.loads(entries[0]['content'])
        request = calls['actions/workflows/c6-bound-economic.yml/dispatches']
        self.assertEqual(envelope['inputs'], request['inputs'])
        self.assertEqual(json.loads(request['inputs']['producer_identity_json'])['binding_id'], 'c6.selected.l2')
        count = len(obj.api.writes)
        with self.assertRaises(ValueError):
            obj.dispatch('c6.base.selected.l4', producer, decision)
        self.assertEqual(len(obj.api.writes), count)

    def test_ambiguous_dispatch_retains_claim_without_blind_retry(self):
        obj, producer, decision = self.make_dispatcher(ambiguous=True)
        with self.assertRaises(TimeoutError):
            obj.dispatch('c6.base.selected.l4', producer, decision)
        self.assertIsNotNone(obj.api.branch)
        with self.assertRaises(ValueError):
            obj.dispatch('c6.base.selected.l4', producer, decision)
        self.assertEqual(sum(path.endswith('/dispatches') for path, _ in obj.api.writes), 1)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]], verbosity=2)
