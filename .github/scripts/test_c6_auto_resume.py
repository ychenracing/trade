"""Synthetic orchestration tests; never import economic source."""
import copy
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import os
import tracemalloc
import zipfile
import unittest
import re
import textwrap
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("resume", pathlib.Path(__file__).with_name("c6_auto_resume.py"))
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.run = {"id": 123, "run_attempt": 1, "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "head_sha": "1" * 40, "head_branch": "codex/c6-v10-workflow-anchor", "display_title": "c6-bound-c6.base.l1-c6-v10-base-l1-a0"}
        self.record = {"record_id": "c6.base.l1", "workflow_binding_id": "c6.base.l1", "logical_run_id": "c6-v10-base-l1", "source_revision": "2" * 40, "candidate_id": "C6-Base", "workflow": {"revision": "1" * 40, "dispatch_ref": self.run["head_branch"]}, "runtime": {"python_version": "3.12.14", "runner_image_os": "ubuntu24", "runner_image_version": "20260831.293.1"}, "attempt_policy": {"dispatch_deadline_utc": "2099-01-01T00:00:00Z"}, "item_manifest_contract": {"count": 3, "sha256": "4" * 64}}
        self.manifest = {key: "" for key in resume.INPUT_NAMES}
        self.manifest.pop("producer_identity_json")
        self.manifest.update(schema_version=2, kind="checkpoint", sealed=True, repository="ychenracing/trade", workflow_run_id="123", workflow_run_attempt="1", source_revision="2" * 40, run_bindings_revision="3" * 40, workflow_revision="1" * 40, binding_id="c6.base.l1", candidate_id="C6-Base", logical_run_id="c6-v10-base-l1", attempt_id="a0", producer_identity={}, fencing_token_sha256="5" * 64, **{k: self.record["runtime"][k] for k in ("python_version", "runner_image_os", "runner_image_version")})
        self.wrapper = {"schema_version": 2, "kind": "c6_bound_checkpoint", "status": "checkpointed_incomplete", "record_id": "c6.base.l1", "source_revision": "2" * 40, "logical_run_id": "c6-v10-base-l1", "attempt_id": "a0", "workflow_run_id": "123", "fencing_sequence": 123, "fencing_token_sha256": "5" * 64, "resume_from": "", "resume_workflow_run_id": "", "child_checkpoint_path": "child-checkpoint.bin", "child_checkpoint_byte_size": 2, "child_checkpoint_full_byte_sha256": hashlib.sha256(b"{}").hexdigest(), "item_manifest_count": 3, "item_manifest_sha256": "4" * 64, "completed_item_ids": ["evaluation/one"], "next_item_ordinal": 1, "completed_item_ids_sha256": hashlib.sha256(b"evaluation/one\n").hexdigest()}

    def build(self, runs=()):
        raw = json.dumps(self.wrapper).encode()
        files = {"checkpoint.json": raw, "child-checkpoint.bin": b"{}"}
        self.manifest["files"] = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
        return resume.build_request(self.manifest, files, self.run, {"binding_records": [self.record]}, list(runs), now="2026-09-05T00:00:00Z")

    def test_exact_resume_and_duplicate_suppression(self):
        request = self.build()
        self.assertEqual(request["inputs"]["attempt_id"], "r1-" + self.manifest["files"]["checkpoint.json"][:12])
        self.assertEqual(request["inputs"]["resume_workflow_run_id"], "123")
        later = copy.deepcopy(self.run)
        later.update(id=124, display_title=self.run["display_title"][:-2] + request["inputs"]["attempt_id"])
        self.assertIsNone(self.build([later]))

    def test_invalid_predecessors_fail_closed(self):
        for field, value in (("status", "in_progress"), ("conclusion", "cancelled"), ("run_attempt", 2), ("head_sha", "0" * 40), ("head_branch", "untrusted")):
            with self.subTest(field=field):
                old = self.run[field]
                self.run[field] = value
                with self.assertRaises(ValueError):
                    self.build()
                self.run[field] = old

    def test_bad_progress_and_identity_fail_closed(self):
        for field, value in (("next_item_ordinal", 0), ("item_manifest_count", 4), ("source_revision", "0" * 40), ("child_checkpoint_full_byte_sha256", "0" * 64)):
            with self.subTest(field=field):
                old = self.wrapper[field]
                self.wrapper[field] = value
                with self.assertRaises(ValueError):
                    self.build()
                self.wrapper[field] = old

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(ValueError):
            resume.decode(b'{"id":1,"id":2}')

    def test_disk_checkpoint_uses_same_dispatch_identity_and_rejects_corruption(self):
        expected = self.build()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            files = {"checkpoint.json": root / "checkpoint.json", "child-checkpoint.bin": root / "child-checkpoint.bin"}
            files["checkpoint.json"].write_bytes(json.dumps(self.wrapper).encode())
            files["child-checkpoint.bin"].write_bytes(b"{}")
            request = resume.build_request(self.manifest, files, self.run, {"binding_records": [self.record]}, [], now="2026-09-05T00:00:00Z")
            self.assertEqual(request, expected)
            files["child-checkpoint.bin"].write_bytes(b"[]")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                resume.build_request(self.manifest, files, self.run, {"binding_records": [self.record]}, [], now="2026-09-05T00:00:00Z")

    def test_strict_metadata_file_decoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "metadata.json"
            for bad in (b'{"x":1,"x":2}', b'{"x":[1,]}', b'{"x":1} trailing'):
                path.write_bytes(bad)
                with self.assertRaises(ValueError):
                    resume.decode(path)
            path.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, "size limit"):
                resume.decode(path)

    @unittest.skipUnless(os.environ.get("C6_STREAM_LARGE_PROBE") == "1", "explicit non-economic capacity probe")
    def test_archive_larger_than_one_gib_remains_bounded(self):
        # Fixed synthetic bytes only: no data/scenario/candidate imports or replay.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive_path = root / "synthetic.zip"
            record = b'{"text":"' + b'x' * (1024 * 1024) + b'"}'
            expected = hashlib.sha256()
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                with archive.open("payload.json", "w", force_zip64=True) as stream:
                    for chunk in (b'{"evaluations":[',):
                        stream.write(chunk)
                        expected.update(chunk)
                    for index in range(1025):
                        chunk = (b',' if index else b'') + record
                        stream.write(chunk)
                        expected.update(chunk)
                    stream.write(b']}\n')
                    expected.update(b']}\n')
                archive.writestr("manifest.json", json.dumps({"files": {"payload.json": expected.hexdigest()}}))
            tracemalloc.start()
            try:
                with patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic"}):
                    github = resume.GitHub()
                with patch.object(resume.urllib.request, "build_opener") as factory:
                    factory.return_value.open.side_effect = lambda *args, **kwargs: archive_path.open("rb")
                    manifest, files = github.artifact(1)
                actual = resume.digest(files["payload.json"])
                self.assertEqual(manifest["files"]["payload.json"], actual)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            size = files["payload.json"].stat().st_size
            self.assertGreater(size, 1024 ** 3)
            self.assertLess(peak, 32 * 1024 * 1024)
            self.assertEqual(actual, expected.hexdigest())
            print(json.dumps({"synthetic_only": True, "expanded_bytes": size, "peak_python_allocations_bytes": peak, "download_extract_sha256_match": True}))
            github._workspace.cleanup()


class ExportWorkflowTest(unittest.TestCase):
    def test_frozen_gzip_transport_stream_hashes_and_plain_l4(self):
        workflow = pathlib.Path(__file__).parents[1] / 'workflows/c6-bound-economic.yml'
        section = workflow.read_text().split('- name: Validate sealed run export', 1)[1]
        source = re.search(r"python - <<'PY'\n(.*?)\n          PY", section, re.S).group(1)
        code = compile(textwrap.dedent(source), str(workflow), 'exec')
        fields = ['source_revision', 'run_bindings_revision', 'workflow_revision', 'binding_id',
                  'candidate_id', 'logical_run_id', 'attempt_id', 'resume_from', 'resume_workflow_run_id',
                  'd_commit', 'd_selection_blob_oid', 'd_selection_file_sha256', 'runner_image_os',
                  'runner_image_version', 'python_version']
        for stage, compressed, matches in [('L1', True, True), ('L1', False, True), ('L1', True, False), ('L4', False, True), ('L4', True, True)]:
            with self.subTest(stage=stage, compressed=compressed, matches=matches), tempfile.TemporaryDirectory() as folder:
                prior = os.getcwd()
                try:
                    os.chdir(folder)
                    root = pathlib.Path('source/artifacts/checkpoints/c6/sealed-export')
                    root.mkdir(parents=True)
                    name = 'payload.json.gz' if compressed else 'official-artifact.json' if stage == 'L4' else 'payload.json'
                    content = b'lossless transport bytes'
                    (root / name).write_bytes(content)
                    (root / 'digest.json').write_text('{}')
                    manifest = {field: '' for field in fields}
                    manifest.update(schema_version=2, kind='result', sealed=True, repository='ychenracing/trade',
                        workflow_run_id='123', workflow_run_attempt='1', binding_id='c6.selected.l4' if stage == 'L4' else 'c6.base.l1',
                        candidate_id='C6-Base', logical_run_id='c6-v11-synthetic', producer_identity={},
                        fencing_token_sha256=hashlib.sha256(b'synthetic-token').hexdigest(),
                        files={name:hashlib.sha256(content).hexdigest(),'digest.json':hashlib.sha256(b'{}').hexdigest()})
                    (root / 'manifest.json').write_text(json.dumps(manifest))
                    bindings = pathlib.Path('bindings/artifacts/diagnostics/c6-run-bindings.json')
                    bindings.parent.mkdir(parents=True)
                    bindings.write_text(json.dumps({'binding_records':[{'workflow_binding_id':manifest['binding_id'],
                        'logical_run_id':manifest['logical_run_id'],'candidate_id':'C6-Base',
                        'paths':{'producer_payload_relative_path':name if matches else 'payload.json'}}]}))
                    env = {'C6_'+field.upper():manifest[field] for field in fields}
                    env.update(C6_EXECUTE_OUTCOME='success',C6_PRODUCER_IDENTITY_JSON='{}',C6_FENCING_TOKEN='synthetic-token',
                        GITHUB_REPOSITORY=manifest['repository'],GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',GITHUB_OUTPUT=str(pathlib.Path(folder)/'output'))
                    with patch.dict(os.environ,env), patch.object(pathlib.Path,'read_bytes',side_effect=AssertionError('whole-file read')):
                        if not matches or (stage == 'L4' and compressed):
                            with self.assertRaises(SystemExit):
                                exec(code, {})
                        else:
                            exec(code, {})
                            self.assertEqual(pathlib.Path(env['GITHUB_OUTPUT']).read_text(),'ready=true\n')
                finally:
                    os.chdir(prior)


if __name__ == "__main__":
    unittest.main()
