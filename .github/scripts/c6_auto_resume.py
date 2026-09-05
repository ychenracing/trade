"""Resume a sealed C6 attempt without importing or executing candidate code."""
import base64
import datetime
import importlib.util
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import tempfile
from pathlib import Path

REPOSITORY = "ychenracing/trade"
WORKFLOW = "c6-bound-economic.yml"
# Load only the reviewed IO leaf from this trusted checkout. Never add the
# candidate checkout or artifact paths to sys.path, and never import the engine.
_io_spec = importlib.util.spec_from_file_location("c6_trusted_io", Path(__file__).resolve().parents[2] / "quantfusion/io/c6_stream.py")
_io = importlib.util.module_from_spec(_io_spec)
_io_spec.loader.exec_module(_io)

INPUT_NAMES = {
    "source_revision", "run_bindings_revision", "workflow_revision", "binding_id",
    "candidate_id", "logical_run_id", "attempt_id", "resume_from",
    "resume_workflow_run_id", "d_commit", "d_selection_blob_oid",
    "d_selection_file_sha256", "producer_identity_json", "runner_image_os",
    "runner_image_version", "python_version",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def decode(raw):
    if isinstance(raw, Path):
        return _io.load_object(raw)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def finite(value):
        raise ValueError("non-finite JSON value")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=finite)


def digest(raw):
    return _io.content_hash(raw)


def build_request(manifest, files, run, bindings, history, *, now):
    require(run["status"] == "completed" and run["conclusion"] == "success", "predecessor did not complete successfully")
    require(run["event"] == "workflow_dispatch" and run["run_attempt"] == 1, "invalid predecessor event or rerun")
    require(manifest["schema_version"] == 2 and manifest["sealed"] is True, "unsealed artifact")
    require(manifest["repository"] == REPOSITORY and manifest["workflow_run_id"] == str(run["id"]) and manifest["workflow_run_attempt"] == "1", "artifact run identity mismatch")
    require(manifest["workflow_revision"] == run["head_sha"], "workflow revision mismatch")
    prefix = f'c6-bound-{manifest["binding_id"]}-{manifest["logical_run_id"]}-'
    require(run["display_title"] == prefix + manifest["attempt_id"], "run title mismatch")
    for key in ("source_revision", "run_bindings_revision", "workflow_revision"):
        require(re.fullmatch(r"[0-9a-f]{40}", manifest[key]), "invalid frozen revision")
    records = [r for r in bindings["binding_records"] if r["logical_run_id"] == manifest["logical_run_id"]]
    require(len(records) == 1, "ambiguous R record")
    record = records[0]
    require(record["workflow"]["dispatch_ref"] == run["head_branch"] and record["workflow"]["revision"] == run["head_sha"], "frozen workflow mismatch")
    require(record["workflow_binding_id"] == manifest["binding_id"], "frozen binding mismatch")
    for key in ("source_revision", "candidate_id"):
        require(record[key] == manifest[key], "frozen source/candidate mismatch")
    for key in ("python_version", "runner_image_os", "runner_image_version"):
        require(record["runtime"][key] == manifest[key], "frozen runtime mismatch")
    require(set(files) == set(manifest["files"]), "artifact file set mismatch")
    require(all(digest(data) == manifest["files"][name] for name, data in files.items()), "artifact file hash mismatch")
    if manifest["kind"] == "result":
        return None
    require(manifest["kind"] == "checkpoint" and set(files) == {"checkpoint.json", "child-checkpoint.bin"}, "invalid checkpoint file set")
    wrapper = decode(files["checkpoint.json"])
    require(wrapper["schema_version"] == 2 and wrapper["kind"] == "c6_bound_checkpoint" and wrapper["status"] == "checkpointed_incomplete", "invalid checkpoint status")
    require(wrapper["record_id"] == record["record_id"], "checkpoint record mismatch")
    for key in ("source_revision", "logical_run_id", "attempt_id", "workflow_run_id", "fencing_token_sha256", "resume_from", "resume_workflow_run_id"):
        require(wrapper[key] == manifest[key], "checkpoint attempt mismatch")
    require(wrapper["fencing_sequence"] == run["id"], "checkpoint fence mismatch")
    child = files["child-checkpoint.bin"]
    require(wrapper["child_checkpoint_path"] == "child-checkpoint.bin" and wrapper["child_checkpoint_byte_size"] == _io.content_size(child) and wrapper["child_checkpoint_full_byte_sha256"] == digest(child), "child checkpoint mismatch")
    completed = wrapper["completed_item_ids"]
    require(isinstance(completed, list) and all(isinstance(x, str) for x in completed) and len(set(completed)) == len(completed), "invalid completed IDs")
    require(type(wrapper["next_item_ordinal"]) is int and wrapper["next_item_ordinal"] == len(completed) and 0 < len(completed) < wrapper["item_manifest_count"], "invalid incomplete progress")
    require(wrapper["completed_item_ids_sha256"] == digest("".join(x + "\n" for x in completed).encode()), "completed prefix hash mismatch")
    contract = record["item_manifest_contract"]
    if contract["count"] is not None:
        require(wrapper["item_manifest_count"] == contract["count"] and wrapper["item_manifest_sha256"] == contract["sha256"], "frozen manifest mismatch")
    require(now <= record["attempt_policy"]["dispatch_deadline_utc"], "dispatch deadline expired")
    # Any later attempt means this event was already consumed. Never create a sibling.
    later = [r for r in history if r["id"] > run["id"] and r.get("display_title", "").startswith(prefix)]
    if later:
        return None
    old = manifest["attempt_id"]
    match = re.fullmatch(r"r([1-9][0-9]*)-[0-9a-f]{12}", old)
    require(old == "a0" or match, "invalid prior attempt identity")
    sequence = 1 if old == "a0" else int(match.group(1)) + 1
    inputs = {key: manifest[key] for key in INPUT_NAMES - {"producer_identity_json"}}
    inputs["producer_identity_json"] = json.dumps(manifest["producer_identity"], sort_keys=True, separators=(",", ":"), allow_nan=False)
    inputs.update(attempt_id=f'r{sequence}-{digest(files["checkpoint.json"])[:12]}', resume_from=digest(files["checkpoint.json"]), resume_workflow_run_id=str(run["id"]))
    require(all(type(x) is str and "\n" not in x and "\r" not in x for x in inputs.values()), "invalid dispatch input")
    return {"ref": record["workflow"]["dispatch_ref"], "inputs": inputs}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self):
        self._workspace = tempfile.TemporaryDirectory(prefix="c6-auto-resume-")
        self.root = f"https://api.github.com/repos/{REPOSITORY}/"
        self.headers = {"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"], "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def read(self, path, *, payload=None):
        request = urllib.request.Request(self.root + path, headers=self.headers, data=None if payload is None else json.dumps(payload).encode(), method="GET" if payload is None else "POST")
        # Never forward the token across a redirect (artifact URLs use a separate request).
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            require(len(raw) <= 16 * 1024 * 1024, "metadata exceeds size limit")
            return decode(raw) if raw else None

    def pages(self, path, key):
        rows = []
        for page in range(1, 11):
            result = self.read(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
            require(result["total_count"] <= 1000, "history exceeds complete API window")
            rows.extend(result[key])
            if len(rows) >= result["total_count"]:
                return rows
        raise ValueError("incomplete paginated history")

    def artifact(self, artifact_id):
        root = Path(self._workspace.name) / str(artifact_id)
        root.mkdir(mode=0o700)
        archive_path = root / "archive.zip"
        request = urllib.request.Request(self.root + f"actions/artifacts/{artifact_id}/zip", headers=self.headers)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            response = opener.open(request, timeout=60)
        except urllib.error.HTTPError as exc:
            require(exc.code == 302, "artifact download failed")
            location = exc.headers["Location"]
            parsed = urllib.parse.urlparse(location)
            require(parsed.scheme == "https" and not parsed.username and not parsed.password and parsed.port in {None, 443} and parsed.hostname and (parsed.hostname.endswith(".blob.core.windows.net") or parsed.hostname.endswith(".actions.githubusercontent.com") or parsed.hostname == "objects.githubusercontent.com"), "untrusted artifact redirect")
            response = opener.open(location, timeout=60)
        with response, archive_path.open("xb") as target:
            _io.copy_stream(response, target)
        files = _io.extract_archive(archive_path, root / "files")
        archive_path.unlink()
        manifest = files.pop("manifest.json", None)
        require(manifest is not None and manifest.stat().st_size <= 1024 * 1024, "missing or oversized manifest")
        return decode(manifest), files


def main():
    require(os.environ["GITHUB_REPOSITORY"] == REPOSITORY and os.environ["GITHUB_EVENT_NAME"] == "workflow_run", "unsupported trigger")
    require(os.environ["GITHUB_RUN_ATTEMPT"] == "1", "native dispatcher reruns are forbidden")
    event = decode(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    require(event["action"] == "completed", "nonterminal event")
    github = GitHub()
    run = github.read(f'actions/runs/{event["workflow_run"]["id"]}')
    require(run["repository"]["full_name"] == REPOSITORY and run["head_repository"]["full_name"] == REPOSITORY, "foreign repository")
    require(run["path"] == ".github/workflows/" + WORKFLOW and run["name"] == "C6 Bound Economic Run", "unexpected workflow")
    require(run["status"] == "completed" and run["conclusion"] == "success", "run has no successful sealed handoff")
    # Restrict automatic execution to the fresh experiment family; old failures stay sealed.
    require(re.fullmatch(r"codex/c6-v(?:1[0-9]|[2-9][0-9]+)-workflow-anchor", run["head_branch"]), "pre-recovery experiment")
    artifacts = github.pages(f'actions/runs/{run["id"]}/artifacts', "artifacts")
    require(len(artifacts) == 1 and not artifacts[0]["expired"], "missing or ambiguous sealed artifact")
    manifest, files = github.artifact(artifacts[0]["id"])
    require(artifacts[0]["name"] == f'c6-bound-{manifest["logical_run_id"]}-{manifest["attempt_id"]}', "artifact name mismatch")
    revision = manifest["run_bindings_revision"]
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "invalid R revision")
    content = github.read(f"contents/artifacts/diagnostics/c6-run-bindings.json?ref={revision}")
    require(content["encoding"] == "base64", "unsupported R encoding")
    bindings = decode(base64.b64decode(content["content"], validate=False))
    history = github.pages(f"actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&branch=" + urllib.parse.quote(run["head_branch"], safe=""), "workflow_runs")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    request = build_request(manifest, files, run, bindings, history, now=now)
    if request is None:
        print("Terminal result or already-dispatched successor; no new attempt.")
        return
    anchor = github.read("git/ref/heads/" + request["ref"])
    require(anchor["object"]["sha"] == request["inputs"]["workflow_revision"], "workflow anchor moved")
    github.read(f"actions/workflows/{WORKFLOW}/dispatches", payload=request)
    print("Dispatched", request["inputs"]["logical_run_id"], request["inputs"]["attempt_id"], "from", run["id"])


if __name__ == "__main__":
    main()
