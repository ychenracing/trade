"""Synthetic producer and full orchestration consumer; no market data."""
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("probe", pathlib.Path(__file__).with_name("c6_resume_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTest(unittest.TestCase):
    def test_sealed_checkpoint_authenticated_successor_and_terminal_no_dispatch(self):
        inputs = {key: "" for key in probe.INPUT_NAMES}
        inputs.update(source_revision="1" * 40, workflow_revision="1" * 40,
                      run_bindings_revision="2" * 40, binding_id="c6.synthetic.resume",
                      logical_run_id="c6-v12-resume-probe-test", candidate_id="synthetic-only",
                      attempt_id="a0", producer_identity_json="{}", python_version="3.12.14",
                      runner_image_os="ubuntu24", runner_image_version="synthetic")
        record = {"record_id": inputs["binding_id"], "workflow_binding_id": inputs["binding_id"],
                  "logical_run_id": inputs["logical_run_id"], "source_revision": inputs["source_revision"],
                  "candidate_id": inputs["candidate_id"],
                  "workflow": {"revision": inputs["workflow_revision"], "dispatch_ref": probe.AUTO_ANCHOR},
                  "runtime": {key: inputs[key] for key in ("python_version", "runner_image_os", "runner_image_version")},
                  "attempt_policy": {"dispatch_deadline_utc": "2099-01-01T00:00:00Z"},
                  "item_manifest_contract": {"count": 2, "sha256": probe.digest(b"control/synthetic-square-1\ncontrol/synthetic-square-2\n")}}
        bindings = {"kind": "c6_synthetic_resume_probe", "binding_records": [record]}
        run = {"id": 123, "run_attempt": 1, "event": "workflow_dispatch", "status": "completed",
               "conclusion": "success", "head_sha": inputs["workflow_revision"], "head_branch": probe.AUTO_ANCHOR,
               "display_title": "c6-bound-c6.synthetic.resume-c6-v12-resume-probe-test-a0",
               "repository": {"full_name": probe.REPOSITORY}, "head_repository": {"full_name": probe.REPOSITORY},
               "path": ".github/workflows/c6-bound-economic.yml", "workflow_id": 42}
        github = Mock()
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder) / "first"
            probe.emit(root, inputs, "123", bindings, github)
            manifest = probe.decode(root / "manifest.json")
            files = {name: root / name for name in manifest["files"]}
            request = probe.build_request(manifest, files, run, bindings, [], now="2026-09-05T00:00:00Z")
            self.assertEqual(request["inputs"]["resume_workflow_run_id"], "123")
            later = {**run, "id": 124, "display_title": run["display_title"][:-2] + request["inputs"]["attempt_id"]}
            self.assertIsNone(probe.build_request(manifest, files, run, bindings, [later], now="2026-09-05T00:00:00Z"))
            github.read.side_effect = [run, {"id": 42, "path": run["path"]}]
            github.pages.return_value = [{"id": 456, "expired": False}]
            github.artifact.return_value = (manifest, files)
            terminal = pathlib.Path(folder) / "last"
            probe.emit(terminal, request["inputs"], "124", bindings, github)
            result = probe.decode(terminal / "payload.json")
            self.assertEqual(result, {"synthetic": True, "completed": [1, 2], "squares": [1, 4], "terminal": True, "economic_evaluations": 0})
            sealed = probe.decode(terminal / "manifest.json")
            self.assertIsNone(probe.build_request(sealed, {"payload.json": terminal / "payload.json"}, later, bindings, [], now="2026-09-05T00:00:00Z"))
            # Even rehashed transport cannot promote invalid synthetic evidence.
            files["child-checkpoint.bin"].write_text(json.dumps({"synthetic": True, "completed": [1], "square": 2}))
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                probe.build_request(manifest, files, run, bindings, [], now="2026-09-05T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
