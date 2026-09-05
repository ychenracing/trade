"""Two synthetic items exercise the real bound dispatch/artifact/resume route."""
import base64
import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from c6_auto_resume import (
    AUTO_ANCHOR,
    INPUT_NAMES,
    REPOSITORY,
    GitHub,
    build_request,
    decode,
    digest,
    require,
    validate_run_identity,
)


def emit(root, inputs, run_id, bindings, github):
    """Use explicit synthetic identities; never import a candidate or market data."""
    require(inputs["binding_id"] == "c6.synthetic.resume" and inputs["logical_run_id"].startswith("c6-v12-resume-probe-"), "not an isolated probe")
    require(inputs["source_revision"] == inputs["workflow_revision"] and bindings["kind"] == "c6_synthetic_resume_probe", "invalid synthetic source contract")
    require(len(bindings["binding_records"]) == 1, "probe has ambiguous binding")
    record = bindings["binding_records"][0]
    require(record["workflow_binding_id"] == inputs["binding_id"] and record["logical_run_id"] == inputs["logical_run_id"], "probe binding mismatch")
    require(record["workflow"] == {"revision": inputs["workflow_revision"], "dispatch_ref": AUTO_ANCHOR}, "probe workflow mismatch")
    require(record["source_revision"] == inputs["source_revision"] and record["candidate_id"] == inputs["candidate_id"], "probe source mismatch")
    require(all(inputs[key] == record["runtime"][key] for key in ("python_version", "runner_image_os", "runner_image_version")), "probe runtime mismatch")
    require(all(not inputs[key] for key in ("d_commit", "d_selection_blob_oid", "d_selection_file_sha256")), "probe cannot contain selection")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = ["control/synthetic-square-1", "control/synthetic-square-2"]
    identity = hashlib.sha256("".join(x + "\n" for x in ids).encode()).hexdigest()
    require(record["item_manifest_contract"] == {"count": 2, "sha256": identity}, "probe manifest mismatch")
    if inputs["attempt_id"] != "a0":
        require(inputs["resume_workflow_run_id"].isdigit(), "probe predecessor missing")
        previous = github.read("actions/runs/" + inputs["resume_workflow_run_id"])
        validate_run_identity(previous, github.read("actions/workflows/c6-bound-economic.yml"))
        artifacts = github.pages(f"actions/runs/{previous['id']}/artifacts", "artifacts")
        require(len(artifacts) == 1 and not artifacts[0]["expired"], "probe checkpoint unavailable")
        manifest, files = github.artifact(artifacts[0]["id"])
        request = build_request(manifest, files, previous, bindings, [], now=now)
        require(request is not None and request["inputs"] == inputs, "probe successor not exact")
        require(inputs["attempt_id"].startswith("r1-"), "probe cannot create a third attempt")
        require(decode(files["child-checkpoint.bin"]) == {"synthetic": True, "completed": [1], "square": 1}, "probe record failed consumer")
        output = {"synthetic": True, "completed": [1, 2], "squares": [1, 4], "terminal": True, "economic_evaluations": 0}
        files = {"payload.json": output}
        kind = "result"
    else:
        require(not inputs["resume_from"] and not inputs["resume_workflow_run_id"], "initial probe cannot resume")
        child = {"synthetic": True, "completed": [1], "square": 1}
        raw = json.dumps(child, sort_keys=True, separators=(",", ":")).encode()
        wrapper = {"schema_version": 2, "kind": "c6_bound_checkpoint", "status": "checkpointed_incomplete",
                   "record_id": record["record_id"], "source_revision": inputs["source_revision"],
                   "logical_run_id": inputs["logical_run_id"], "attempt_id": "a0", "workflow_run_id": run_id,
                   "fencing_sequence": int(run_id), "fencing_token_sha256": digest(("synthetic:" + run_id).encode()),
                   "resume_from": "", "resume_workflow_run_id": "", "child_checkpoint_path": "child-checkpoint.bin",
                   "child_checkpoint_byte_size": len(raw), "child_checkpoint_full_byte_sha256": digest(raw),
                   "item_manifest_count": 2, "item_manifest_sha256": identity, "completed_item_ids": ids[:1],
                   "completed_item_ids_sha256": digest((ids[0] + "\n").encode()), "next_item_ordinal": 1}
        files = {"checkpoint.json": wrapper, "child-checkpoint.bin": child}
        kind = "checkpoint"
    root.mkdir()
    for name, value in files.items():
        (root / name).write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    manifest = {key: value for key, value in inputs.items() if key != "producer_identity_json"}
    manifest.update(schema_version=2, kind=kind, sealed=True, repository=REPOSITORY,
                    workflow_run_id=run_id, workflow_run_attempt="1", producer_identity=decode(inputs["producer_identity_json"]),
                    fencing_token_sha256=digest(("synthetic:" + run_id).encode()),
                    files={name: digest(root / name) for name in files})
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    print(json.dumps({"synthetic": True, "kind": kind, "completed": 1 if kind == "checkpoint" else 2, "economic_evaluations": 0}))


def main():
    require(os.environ["GITHUB_REPOSITORY"] == REPOSITORY and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
            and os.environ["GITHUB_RUN_ATTEMPT"] == "1", "invalid probe invocation")
    inputs = {key: str(decode(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())["inputs"].get(key, "")) for key in INPUT_NAMES}
    for key in ("source_revision", "run_bindings_revision", "workflow_revision"):
        require(re.fullmatch("[0-9a-f]{40}", inputs[key]), "probe revision missing")
    require(subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == inputs["workflow_revision"], "probe checkout drift")
    require(os.environ["GITHUB_SHA"] == inputs["workflow_revision"]
            and os.environ["GITHUB_REF_NAME"] == AUTO_ANCHOR, "probe workflow execution drift")
    require(platform.python_version() == inputs["python_version"]
            and os.environ["ImageOS"] == inputs["runner_image_os"]
            and os.environ["ImageVersion"] == inputs["runner_image_version"], "probe execution environment drift")
    github = GitHub()
    content = github.read("contents/artifacts/diagnostics/c6-run-bindings.json?ref=" + inputs["run_bindings_revision"])
    bindings = decode(base64.b64decode(content["content"]))
    emit(Path("probe-export"), inputs, os.environ["GITHUB_RUN_ID"], bindings, github)


if __name__ == "__main__":
    main()
