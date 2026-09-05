"""Synthetic orchestration tests; never import economic source."""
import copy
import hashlib
import importlib.util
import json
import pathlib
import unittest

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


if __name__ == "__main__":
    unittest.main()
