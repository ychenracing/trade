"""Transport-only regression for sealed exports coexisting with core shard artifacts."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('artifact_relay', HERE / 'c6_stage_advance.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class ArtifactSelectionTests(unittest.TestCase):
    def setUp(self):
        self.record = {'workflow_binding_id': 'c6.base.l1', 'logical_run_id': 'c6-v27-base-l1',
                       'stage': 'L1', 'source_revision': relay.BASE, 'candidate_id': 'C6-Base',
                       'runtime': {'runner_image_os': 'ubuntu24', 'runner_image_version': 'synthetic',
                                   'python_version': '3.12.14'}}
        self.run = {'id': 42, 'status': 'completed', 'conclusion': 'success', 'run_attempt': 1,
                    'event': 'workflow_dispatch', 'path': '.github/workflows/c6-bound-economic.yml',
                    'workflow_id': 349948458, 'head_branch': relay.ANCHOR, 'head_sha': relay.WORKFLOW,
                    'display_title': 'c6-bound-c6.base.l1-c6-v27-base-l1-a0'}
        self.seal = {'id': 100, 'name': 'c6-bound-c6-v27-base-l1-a0'}
        self.shards = [{'id': i, 'name': f'c6-core-c6-v27-base-l1-a0-shard-{i}'} for i in range(12)]
        self.artifacts = self.shards[:6] + [self.seal] + self.shards[6:]
        self.manifest = {'kind': 'checkpoint', 'repository': relay.REPOSITORY,
                         'workflow_run_id': '42', 'workflow_run_attempt': '1',
                         'binding_id': 'c6.base.l1', 'logical_run_id': 'c6-v27-base-l1',
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
                          self.shards + [dict(self.seal, name='c6-bound-c6-v27-base-l1-a1')]):
            with self.subTest(artifacts=artifacts):
                self.artifacts = artifacts
                with self.assertRaisesRegex(ValueError, 'missing/ambiguous producer artifact'):
                    self.read()
                self.assertEqual(self.downloads, [])

    def test_single_export_still_supported(self):
        self.artifacts = [self.seal]
        self.assertIsNone(self.read())
        self.assertEqual(self.downloads, [(42, self.seal)])

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


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]], verbosity=2)
