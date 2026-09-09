"""Transport-only regression for sealed exports coexisting with core shard artifacts."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('artifact_relay', HERE / 'c6_stage_advance.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class WorkflowBudgetTests(unittest.TestCase):
    def test_advance_budget_covers_full_native_result_validation(self):
        workflow = (HERE.parent / 'workflows' / 'c6-stage-advance.yml').read_text()
        advance = workflow.split('\n  advance:\n', 1)[1]
        self.assertIn('\n    timeout-minutes: 360\n', advance)


class ArtifactSelectionTests(unittest.TestCase):
    def setUp(self):
        self.record = {'workflow_binding_id': 'c6.base.l1', 'logical_run_id': 'c6-v30-base-l1',
                       'stage': 'L1', 'source_revision': relay.BASE, 'candidate_id': 'C6-Base',
                       'runtime': {'runner_image_os': 'ubuntu24', 'runner_image_version': 'synthetic',
                                   'python_version': '3.12.14'}}
        self.run = {'id': 42, 'status': 'completed', 'conclusion': 'success', 'run_attempt': 1,
                    'event': 'workflow_dispatch', 'path': '.github/workflows/c6-bound-economic.yml',
                    'workflow_id': 349948458, 'head_branch': relay.ANCHOR, 'head_sha': relay.WORKFLOW,
                    'display_title': 'c6-bound-c6.base.l1-c6-v30-base-l1-a0'}
        self.seal = {'id': 100, 'name': 'c6-bound-c6-v30-base-l1-a0'}
        self.shards = [{'id': i, 'name': f'c6-core-c6-v30-base-l1-a0-shard-{i}'} for i in range(12)]
        self.artifacts = self.shards[:6] + [self.seal] + self.shards[6:]
        self.manifest = {'kind': 'checkpoint', 'repository': relay.REPOSITORY,
                         'workflow_run_id': '42', 'workflow_run_attempt': '1',
                         'binding_id': 'c6.base.l1', 'logical_run_id': 'c6-v30-base-l1',
                         'attempt_id': 'a0', 'source_revision': relay.BASE, 'candidate_id': 'C6-Base',
                         'run_bindings_revision': relay.R_COMMIT, 'workflow_revision': relay.WORKFLOW,
                         **self.record['runtime']}
        self.downloads = []
        self.instance = relay.Relay.__new__(relay.Relay)
        self.instance.records = {'c6.base.l1': self.record}
        self.instance.cache, self.instance.exports = {}, []
        self.instance.store = SimpleNamespace(_pages=lambda *args: self.artifacts, _export=self.export)

    def export(self, run_id, artifact):
        self.downloads.append((run_id, artifact))
        return SimpleNamespace(manifest=self.manifest)

    def read(self):
        return self.instance.read_result('c6.base.l1', [self.run])

    def test_twelve_shards_and_one_seal_select_only_seal(self):
        self.assertIsNone(self.read())  # A checkpoint is not promoted to a result.
        self.assertEqual(self.downloads, [(42, self.seal)])

    def test_missing_duplicate_and_wrong_attempt_fail_before_download(self):
        for artifacts in ([], self.shards, self.artifacts + [dict(self.seal, id=101)],
                          self.shards + [dict(self.seal, name='c6-bound-c6-v30-base-l1-a1')]):
            with self.subTest(artifacts=artifacts), self.assertRaisesRegex(ValueError, 'missing/ambiguous producer artifact'):
                self.artifacts = artifacts
                self.read()
            self.assertEqual(self.downloads, [])

    def test_single_export_still_supported(self):
        self.artifacts = [self.seal]
        self.assertIsNone(self.read())
        self.assertEqual(self.downloads, [(42, self.seal)])

    def test_truncated_transport_is_retried_once_before_validation(self):
        def flaky_export(run_id, artifact):
            self.downloads.append((run_id, artifact))
            if len(self.downloads) == 1:
                raise zipfile.BadZipFile('truncated transport')
            return SimpleNamespace(manifest=self.manifest)

        self.instance.store._export = flaky_export
        self.assertIsNone(self.read())
        self.assertEqual(self.downloads, [(42, self.seal), (42, self.seal)])

    def test_checkpoint_successor_uses_exact_attempt_not_a0(self):
        attempt = 'r1-' + 'a' * 12
        self.run['display_title'] = self.run['display_title'].removesuffix('a0') + attempt
        self.seal['name'] = self.seal['name'].removesuffix('a0') + attempt
        self.manifest['attempt_id'] = attempt
        self.assertIsNone(self.read())
        self.assertEqual(self.downloads, [(42, self.seal)])

    def test_selected_export_keeps_frozen_manifest_identity_checks(self):
        original = copy.deepcopy(self.manifest)
        for key in ('source_revision', 'workflow_revision', 'run_bindings_revision',
                    'workflow_run_id', 'logical_run_id', 'attempt_id'):
            with self.subTest(key=key):
                self.manifest = dict(original, **{key: 'forged'})
                with self.assertRaises(ValueError):
                    self.read()


# Reuse the existing real compressed-payload/schema/formula/digest tests in CI,
# adding the twelve sibling artifacts at the actual API listing boundary.
if (HERE / 'test_c6_stage_advance.py').exists():
    native_spec = importlib.util.spec_from_file_location('native_stage_tests', HERE / 'test_c6_stage_advance.py')
    native = importlib.util.module_from_spec(native_spec)
    native_spec.loader.exec_module(native)

    class NativeMultiArtifactTests(native.NativeExportTests):
        def setUp(self):
            super().setUp()
            original = self.instance.store._pages
            siblings = [{'name': f'c6-core-synthetic-shard-{i}'} for i in range(12)]
            self.instance.store._pages = lambda *args: original(*args) + siblings


class AutoResumeSelectionTests(unittest.TestCase):
    """Exercise the actual resume entrypoint up to its unchanged sealed consumer."""
    def setUp(self):
        import tempfile
        import json
        from unittest.mock import patch
        auto_spec = importlib.util.spec_from_file_location('auto_listing', HERE / 'c6_auto_resume.py')
        self.auto = importlib.util.module_from_spec(auto_spec)
        auto_spec.loader.exec_module(self.auto)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        event = Path(self.tmp.name) / 'event.json'
        event.write_text(json.dumps({'action': 'completed', 'workflow_run': {'id': 42}}))
        env = patch.dict(self.auto.os.environ, {'GITHUB_REPOSITORY': relay.REPOSITORY,
                         'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_RUN_ATTEMPT': '1',
                         'GITHUB_EVENT_PATH': str(event)})
        env.start()
        self.addCleanup(env.stop)
        self.run = {'id': 42, 'status': 'completed', 'conclusion': 'success', 'run_attempt': 1,
                    'event': 'workflow_dispatch', 'head_branch': self.auto.AUTO_ANCHOR,
                    'workflow_id': 349948458, 'path': '.github/workflows/c6-bound-economic.yml',
                    'repository': {'full_name': relay.REPOSITORY},
                    'head_repository': {'full_name': relay.REPOSITORY},
                    'display_title': 'c6-bound-c6.base.l1-c6-v30-base-l1-a0'}
        self.seal = {'id': 100, 'name': 'c6-bound-c6-v30-base-l1-a0', 'expired': False}
        self.shards = [{'id': i, 'name': f'c6-core-shard-{i}', 'expired': False} for i in range(12)]
        self.artifacts = self.shards + [self.seal]
        self.downloads = []
        fake = SimpleNamespace(read=self.read, pages=lambda *args: self.artifacts, artifact=self.artifact)
        factory = patch.object(self.auto, 'GitHub', return_value=fake)
        factory.start()
        self.addCleanup(factory.stop)

    class SealReached(Exception):
        pass

    def read(self, path):
        if path == 'actions/runs/42':
            return self.run
        if path == 'actions/workflows/c6-bound-economic.yml':
            return {'id': 349948458, 'path': self.run['path']}
        raise AssertionError('unexpected API read: ' + path)

    def artifact(self, artifact_id):
        self.downloads.append(artifact_id)
        raise self.SealReached  # Network boundary only; no economic payload fixture.

    def test_multi_artifact_result_and_successor_reach_only_exact_seal(self):
        for attempt in ('a0', 'r1-' + 'a' * 12):
            with self.subTest(attempt=attempt):
                self.run['display_title'] = 'c6-bound-c6.base.l1-c6-v30-base-l1-' + attempt
                self.seal['name'] = 'c6-bound-c6-v30-base-l1-' + attempt
                self.downloads.clear()
                with self.assertRaises(self.SealReached):
                    self.auto.main()
                self.assertEqual(self.downloads, [100])

    def test_missing_duplicate_expired_and_wrong_attempt_fail_closed(self):
        for artifacts in ([], self.shards, self.artifacts + [dict(self.seal, id=101)],
                          self.shards + [dict(self.seal, expired=True)],
                          self.shards + [dict(self.seal, name='c6-bound-c6-v30-base-l1-a1')]):
            with self.subTest(artifacts=artifacts):
                self.artifacts = artifacts
                with self.assertRaisesRegex(ValueError, 'missing or ambiguous sealed artifact'):
                    self.auto.main()
                self.assertEqual(self.downloads, [])

    def test_native_rerun_and_foreign_repository_still_fail_before_download(self):
        for changes in ({'run_attempt': 2}, {'head_repository': {'full_name': 'foreign/repo'}},
                        {'display_title': 'malformed'}, {'display_title': 'c6-bound--wrong'}):
            with self.subTest(changes=changes):
                original = dict(self.run)
                self.run.update(changes)
                with self.assertRaises(ValueError):
                    self.auto.main()
                self.assertEqual(self.downloads, [])
                self.run = original


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]], verbosity=2)
