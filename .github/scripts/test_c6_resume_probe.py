"""Synthetic producer and full orchestration consumer; no market data."""
import importlib.util
import json
import pathlib
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("probe", pathlib.Path(__file__).with_name("c6_resume_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTest(unittest.TestCase):
    def test_actual_dispatch_envelope_accepts_independent_execution_version(self):
        workflow = pathlib.Path(__file__).parents[1] / "workflows/c6-dispatch.yml"
        source = workflow.read_text().split("python3 -I -S - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        code = compile(textwrap.dedent(source), str(workflow), "exec")
        inputs = {key: "" for key in probe.INPUT_NAMES}
        inputs.update(source_revision="1" * 40, run_bindings_revision="2" * 40,
                      workflow_revision="3" * 40, binding_id="c6.synthetic.resume",
                      candidate_id="C6-Base", logical_run_id="c6-v18-resume-probe-cross",
                      attempt_id="a0", runner_image_os="ubuntu24",
                      runner_image_version="synthetic", python_version="3.12.14",
                      producer_identity_json=json.dumps({key: "" for key in (
                          "artifact_full_byte_sha256", "attempt_id", "binding_id",
                          "logical_run_id", "workflow_run_id")}, sort_keys=True, separators=(",", ":")))
        request = {"schema_version": 1, "workflow_ref": "codex/c6-v17-workflow-anchor", "inputs": inputs}
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / ".github").mkdir()
            event = root / "event.json"
            event.write_text(json.dumps({"created": True, "deleted": False, "before": "0" * 40}))
            env = {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event),
                   "GITHUB_SHA": "4" * 40, "GITHUB_REPOSITORY": probe.REPOSITORY,
                   "GITHUB_REF": "refs/heads/codex/c6-dispatch/c6-v18-resume-probe-cross/c6.synthetic.resume/a0",
                   "RUNNER_TEMP": str(root)}
            replies = {
                "rev-parse": "4" * 40 + "\n",
                "rev-list": "4" * 40 + " " + "3" * 40 + "\n",
                "diff": "A\t.github/c6-dispatch-request.json\n",
                "ls-tree": "100644 blob " + "5" * 40 + "\t.github/c6-dispatch-request.json\n",
                "ls-remote": "3" * 40 + "\trefs/heads/codex/c6-v17-workflow-anchor\n",
            }
            original = pathlib.Path.cwd()
            try:
                probe.os.chdir(root)
                with patch.dict(probe.os.environ, env), patch("subprocess.check_output", side_effect=lambda args, **kw: replies[args[1]]):
                    for ref in ("codex/c6-v17-workflow-anchor", "codex/c6-v18-workflow-anchor"):
                        request["workflow_ref"] = ref
                        (root / ".github/c6-dispatch-request.json").write_text(
                            json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
                        if ref.endswith("v17-workflow-anchor"):
                            exec(code, {})
                            self.assertEqual(json.loads((root / "c6-dispatch-payload.json").read_text())["ref"], ref)
                        else:
                            with self.assertRaises(SystemExit):
                                exec(code, {})
            finally:
                probe.os.chdir(original)

    def test_completion_requests_only_native_dispatch_at_exact_workflow_anchor(self):
        spec = importlib.util.spec_from_file_location("request", pathlib.Path(__file__).with_name("c6_request_resume.py"))
        request = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(request)
        github = Mock()
        github.read.return_value = {"object": {"sha": "1" * 40}}
        with tempfile.TemporaryDirectory() as folder:
            event = pathlib.Path(folder) / "event.json"
            event.write_text(json.dumps({"inputs": {"workflow_revision": "1" * 40}}))
            env = {"GITHUB_REPOSITORY": probe.REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
                   "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": "1" * 40,
                   "GITHUB_REF_NAME": probe.AUTO_ANCHOR, "GITHUB_RUN_ID": "123"}
            with patch.dict(request.os.environ, env), patch.object(request, "GitHub", return_value=github):
                request.main()
                github.read.assert_called_with("actions/workflows/c6-auto-resume.yml/dispatches", payload={
                    "ref": probe.AUTO_ANCHOR, "inputs": {"predecessor_run_id": "123"}})
                with patch.dict(request.os.environ, {"GITHUB_RUN_ATTEMPT": "2"}), self.assertRaises(ValueError):
                    request.main()
                with patch.dict(request.os.environ, {"GITHUB_SHA": "2" * 40}), self.assertRaises(ValueError):
                    request.main()

    def test_sealed_checkpoint_authenticated_successor_and_terminal_no_dispatch(self):
        inputs = {key: "" for key in probe.INPUT_NAMES}
        inputs.update(source_revision="1" * 40, workflow_revision="1" * 40,
                      run_bindings_revision="2" * 40, binding_id="c6.synthetic.resume",
                      logical_run_id="c6-v18-resume-probe-test", candidate_id="synthetic-only",
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
               "display_title": "c6-bound-c6.synthetic.resume-c6-v18-resume-probe-test-a0",
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
