"""Immutable checkpoint and fencing substrate for C6 bound executions.

The helpers here contain no economic logic.  A frozen R record supplies the
actual command; this module only protects identity, ownership, and publication.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Never, Sequence

from quantfusion.application.c6_contract import (
    ContractError,
    canonical_json_bytes,
    canonical_payload_hash,
    file_sha256,
    load_preregistration,
    load_run_bindings,
    require_exact_keys,
    render_bound_argv,
    select_binding,
    strict_json_load,
    strict_json_loads,
    validate_selection_commit,
)


class BoundRunError(RuntimeError):
    """A lease, checkpoint, or sealed export violated the frozen contract."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{15,255}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEASE_KEYS = frozenset(
    {
        "schema_version",
        "logical_run_id",
        "attempt_id",
        "fencing_token_sha256",
        "fencing_sequence",
        "active",
        "terminal",
        "resume_from",
        "checkpoint_id",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {
        "schema_version", "kind", "status", "record_id", "binding_signature",
        "source_revision", "logical_run_id", "attempt_id", "workflow_run_id",
        "fencing_sequence", "fencing_token_sha256", "resume_from",
        "resume_workflow_run_id", "child_checkpoint_kind",
        "child_checkpoint_path", "child_checkpoint_byte_size",
        "child_checkpoint_full_byte_sha256", "item_manifest_count",
        "item_manifest_sha256", "completed_item_ids",
        "completed_item_ids_sha256", "next_item_ordinal", "created_at",
    }
)
_EXPORT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "sealed",
        "source_revision",
        "run_bindings_revision",
        "workflow_revision",
        "binding_id",
        "candidate_id",
        "logical_run_id",
        "attempt_id",
        "repository",
        "workflow_run_id",
        "workflow_run_attempt",
        "resume_from",
        "resume_workflow_run_id",
        "d_commit",
        "d_selection_blob_oid",
        "d_selection_file_sha256",
        "producer_identity",
        "runner_image_os",
        "runner_image_version",
        "python_version",
        "fencing_token_sha256",
        "files",
    }
)


def _raise(message: str, exc: Exception | None = None) -> Never:
    error = BoundRunError(message)
    if exc is None:
        raise error
    raise error from exc


def _atomic_bytes(path: Path, content: bytes, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if not replace and path.exists():
            _raise(f"immutable path already exists: {path}")
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_object(path: Path, keys: set[str] | frozenset[str], label: str) -> dict[str, Any]:
    try:
        payload = strict_json_load(path)
    except ContractError as exc:
        _raise(f"invalid {label}: {exc}", exc)
    if not isinstance(payload, dict):
        _raise(f"{label} root must be an object")
    try:
        require_exact_keys(payload, keys, label=label)
    except ContractError as exc:
        _raise(str(exc), exc)
    return payload


def _validate_id(value: str, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _raise(f"invalid {label}")


def _validate_token(value: str) -> None:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        _raise("invalid fencing token")


@dataclass(frozen=True)
class ExclusiveLease:
    path: Path
    logical_run_id: str
    attempt_id: str
    fencing_token_sha256: str
    fencing_sequence: int

    @classmethod
    def acquire(
        cls,
        path: str | Path,
        *,
        logical_run_id: str,
        attempt_id: str,
        fencing_token: str,
        fencing_sequence: int,
        resume_from: str = "",
    ) -> "ExclusiveLease":
        """Atomically acquire or monotonically take over an inactive lease."""
        _validate_id(logical_run_id, "logical_run_id")
        _validate_id(attempt_id, "attempt_id")
        _validate_token(fencing_token)
        if type(fencing_sequence) is not int or fencing_sequence < 1:
            _raise("fencing_sequence must be a positive integer")
        lease_path = Path(path)
        token_hash = hashlib.sha256(fencing_token.encode("utf-8")).hexdigest()
        prior: dict[str, Any] | None = None
        if lease_path.exists():
            prior = _load_object(lease_path, _LEASE_KEYS, "lease")
            if prior["logical_run_id"] != logical_run_id:
                _raise("lease belongs to a different logical run")
            if prior["active"] is True:
                _raise("active lease already exists")
            if prior["terminal"] is not True:
                _raise("prior attempt is not terminal or inactive")
            if fencing_sequence <= prior["fencing_sequence"]:
                _raise("fencing sequence is not monotonic")
            if not resume_from or resume_from != prior["checkpoint_id"]:
                _raise("resume_from does not match the sealed checkpoint")
        elif resume_from:
            _raise("resume_from supplied without a prior attempt")
        payload = {
            "schema_version": 1,
            "logical_run_id": logical_run_id,
            "attempt_id": attempt_id,
            "fencing_token_sha256": token_hash,
            "fencing_sequence": fencing_sequence,
            "active": True,
            "terminal": False,
            "resume_from": resume_from,
            "checkpoint_id": None,
        }
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        if prior is None:
            try:
                descriptor = os.open(
                    lease_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(canonical_json_bytes(payload))
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError as exc:
                _raise("active lease already exists", exc)
        else:
            # Re-read immediately before replace so another takeover cannot be hidden.
            if _load_object(lease_path, _LEASE_KEYS, "lease") != prior:
                _raise("lease changed during takeover")
            _atomic_bytes(lease_path, canonical_json_bytes(payload), replace=True)
        return cls(
            path=lease_path,
            logical_run_id=logical_run_id,
            attempt_id=attempt_id,
            fencing_token_sha256=token_hash,
            fencing_sequence=fencing_sequence,
        )

    def assert_current(self) -> None:
        payload = _load_object(self.path, _LEASE_KEYS, "lease")
        expected = (
            self.logical_run_id,
            self.attempt_id,
            self.fencing_token_sha256,
            self.fencing_sequence,
        )
        actual = (
            payload["logical_run_id"],
            payload["attempt_id"],
            payload["fencing_token_sha256"],
            payload["fencing_sequence"],
        )
        if actual != expected or payload["active"] is not True:
            _raise("stale fenced writer")

    def release(self, *, terminal: bool, checkpoint_id: str | None = None) -> None:
        self.assert_current()
        if not terminal and checkpoint_id is not None:
            _raise("inactive nonterminal lease cannot publish a checkpoint")
        payload = _load_object(self.path, _LEASE_KEYS, "lease")
        payload["active"] = False
        payload["terminal"] = terminal
        payload["checkpoint_id"] = checkpoint_id
        _atomic_bytes(self.path, canonical_json_bytes(payload), replace=True)


@dataclass(frozen=True)
class RemoteExport:
    run_id: int
    manifest: dict[str, Any]
    manifest_bytes: bytes
    files: dict[str, bytes]


class GitHubActionsLeaseStore:
    """Fail closed over the complete frozen-workflow history for one logical run."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        opener: Any = urllib.request.urlopen,
    ) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
            _raise("invalid GitHub repository")
        parsed = urllib.parse.urlparse(api_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path.rstrip("/"):
            _raise("invalid GitHub API URL")
        if not token:
            _raise("GitHub history requires a token")
        self.repository, self.token, self.api_url = repository, token, api_url.rstrip("/")
        self.host, self.opener = parsed.netloc, opener

    def _read(self, url: str, *, artifact: bool = False) -> bytes:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != self.host:
            _raise("GitHub returned an untrusted URL")
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            if artifact and self.opener is urllib.request.urlopen:
                try:
                    with urllib.request.build_opener(_NoRedirect).open(request, timeout=60) as response:
                        return response.read()
                except urllib.error.HTTPError as redirect:
                    if redirect.code != 302 or not redirect.headers.get("Location"):
                        raise
                    location = str(redirect.headers["Location"])
                target = urllib.parse.urlparse(location)
                trusted = target.scheme == "https" and (target.netloc.endswith(".blob.core.windows.net") or target.netloc.endswith(".actions.githubusercontent.com") or target.netloc == "objects.githubusercontent.com")
                if not trusted:
                    _raise("GitHub artifact redirected to an untrusted host")
                with urllib.request.urlopen(urllib.request.Request(location, headers={"Accept": "application/zip"}), timeout=60) as response:  # nosec B310
                    return response.read()
            with self.opener(request, timeout=60) as response:
                final = urllib.parse.urlparse(response.geturl())
                if final.scheme != "https" or final.netloc != self.host:
                    _raise("GitHub request redirected to an untrusted host")
                return response.read()
        except Exception as exc:
            _raise("GitHub Actions history request failed", exc)

    def _json(self, url: str, label: str) -> dict[str, Any]:
        try:
            value = strict_json_loads(self._read(url))
        except ContractError as exc:
            _raise(f"invalid GitHub {label} JSON", exc)
        if not isinstance(value, dict):
            _raise(f"GitHub {label} must be an object")
        return value

    def _pages(self, suffix: str, key: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for page in range(1, 101):
            joiner = "&" if "?" in suffix else "?"
            payload = self._json(
                f"{self.api_url}/repos/{self.repository}/{suffix}"
                f"{joiner}per_page=100&page={page}", key,
            )
            values = payload.get(key)
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                _raise(f"GitHub {key} page is malformed")
            found.extend(values)
            if len(values) < 100:
                return found
        _raise(f"GitHub {key} pagination exceeded safety bound")

    def _export(self, run_id: int, artifact: Mapping[str, Any]) -> RemoteExport:
        if artifact.get("expired") is not False or type(artifact.get("id")) is not int:
            _raise("GitHub artifact identity is invalid")
        url = artifact.get("archive_download_url")
        if not isinstance(url, str):
            _raise("GitHub artifact download URL is invalid")
        files: dict[str, bytes] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(self._read(url, artifact=True))) as archive:
                for info in archive.infolist():
                    pure = PurePosixPath(info.filename)
                    mode = (info.external_attr >> 16) & 0o177777
                    if (
                        info.is_dir() or pure.is_absolute() or ".." in pure.parts
                        or pure.as_posix() != info.filename
                        or any(part.startswith(".") for part in pure.parts)
                        or (mode & 0o170000) not in {0, 0o100000}
                        or mode & 0o111 or info.filename in files
                    ):
                        _raise("GitHub artifact ZIP contains an unsafe path")
                    files[info.filename] = archive.read(info)
        except (zipfile.BadZipFile, RuntimeError) as exc:
            _raise("GitHub artifact is not a valid ZIP", exc)
        manifest_bytes = files.pop("manifest.json", None)
        if manifest_bytes is None:
            _raise("GitHub artifact has no manifest.json")
        try:
            manifest = strict_json_loads(manifest_bytes)
        except ContractError as exc:
            _raise("GitHub artifact manifest is invalid JSON", exc)
        if not isinstance(manifest, dict):
            _raise("GitHub artifact manifest must be an object")
        try:
            require_exact_keys(manifest, _EXPORT_KEYS, label="remote manifest")
        except ContractError as exc:
            _raise(str(exc), exc)
        hashes = manifest["files"]
        if manifest["schema_version"] != 2 or manifest["sealed"] is not True:
            _raise("remote manifest is not sealed v2")
        if not isinstance(hashes, dict) or set(hashes) != set(files):
            _raise("remote artifact file map is incomplete")
        if any(hashes[name] != hashlib.sha256(data).hexdigest() for name, data in files.items()):
            _raise("remote artifact file hash mismatch")
        return RemoteExport(run_id, manifest, manifest_bytes, files)

    def restore(
        self, *, binding: Mapping[str, Any], current_run_id: int,
        resume_from: str, resume_workflow_run_id: str, checkpoint_path: Path,
        lease_path: Path, run_bindings_revision: str, item_ids: Sequence[str],
    ) -> None:
        """Restore the exact latest checkpoint, or reject any ambiguous history."""
        workflow = binding["workflow"]
        prefix = f"c6-bound-{binding['workflow_binding_id']}-{binding['logical_run_id']}-"
        encoded = urllib.parse.quote(".github/workflows/c6-bound-economic.yml", safe="")
        runs = self._pages(f"actions/workflows/{encoded}/runs?event=workflow_dispatch", "workflow_runs")
        current = self._json(
            f"{self.api_url}/repos/{self.repository}/actions/runs/{current_run_id}",
            "current run",
        )
        if (
            current.get("event") != "workflow_dispatch"
            or current.get("head_branch") != workflow["dispatch_ref"]
            or current.get("head_sha") != workflow["revision"]
            or current.get("run_attempt") != 1
        ):
            _raise("current workflow identity is invalid")
        prior: list[tuple[dict[str, Any], RemoteExport]] = []
        for summary in runs:
            run_id = summary.get("id")
            if type(run_id) is not int or run_id == current_run_id:
                continue
            if not str(summary.get("display_title", "")).startswith(prefix):
                continue
            detail = self._json(
                f"{self.api_url}/repos/{self.repository}/actions/runs/{run_id}", "run"
            )
            if (
                run_id >= current_run_id
                or detail.get("event") != "workflow_dispatch"
                or detail.get("head_branch") != workflow["dispatch_ref"]
                or detail.get("head_sha") != workflow["revision"]
                or detail.get("run_attempt") != 1
                or detail.get("status") != "completed"
                or str(detail.get("created_at", "")) > binding["attempt_policy"]["dispatch_deadline_utc"]
            ):
                _raise("prior logical-run workflow identity is invalid")
            artifacts = self._pages(f"actions/runs/{run_id}/artifacts", "artifacts")
            named = [a for a in artifacts if str(a.get("name", "")).startswith(
                f"c6-bound-{binding['logical_run_id']}-")]
            if len(named) != 1:
                _raise("prior logical run lacks one exact sealed artifact")
            prior.append((detail, self._export(run_id, named[0])))
        prior.sort(key=lambda item: item[1].run_id)
        if not prior:
            if resume_from or resume_workflow_run_id:
                _raise("resume inputs supplied without prior history")
            return
        previous = ""
        for _, export in prior:
            manifest = export.manifest
            expected = {
                "logical_run_id": binding["logical_run_id"],
                "binding_id": binding["workflow_binding_id"],
                "source_revision": binding["source_revision"],
                "workflow_revision": workflow["revision"],
                "run_bindings_revision": run_bindings_revision,
                "workflow_run_id": str(export.run_id),
            }
            if any(manifest[key] != value for key, value in expected.items()):
                _raise("prior artifact identity mismatch")
            if manifest["resume_from"] != previous:
                _raise("prior checkpoint chain is broken")
            if manifest["kind"] == "result":
                _raise("logical run already has a terminal result")
            wrapper = export.files.get("checkpoint.json")
            child = export.files.get("child-checkpoint.bin")
            if set(export.files) != {"checkpoint.json", "child-checkpoint.bin"} or wrapper is None or child is None:
                _raise("prior checkpoint file set is invalid")
            wrapper_payload = strict_json_loads(wrapper)
            if not isinstance(wrapper_payload, dict):
                _raise("prior checkpoint wrapper must be an object")
            try:
                require_exact_keys(
                    wrapper_payload, _CHECKPOINT_KEYS, label="checkpoint wrapper"
                )
            except ContractError as exc:
                _raise(str(exc), exc)
            completed_ids = wrapper_payload["completed_item_ids"]
            item_contract = binding["item_manifest_contract"]
            child_completed = checkpoint_progress(child, stage=binding["stage"], binding_signature=wrapper_payload["binding_signature"], item_ids=item_ids)
            if (
                wrapper_payload["schema_version"] != 2
                or wrapper_payload["kind"] != "c6_bound_checkpoint"
                or wrapper_payload["status"] != "checkpointed_incomplete"
                or wrapper_payload["record_id"] != binding["record_id"]
                or wrapper_payload["source_revision"] != binding["source_revision"]
                or wrapper_payload["logical_run_id"] != binding["logical_run_id"]
                or wrapper_payload["attempt_id"] != manifest["attempt_id"]
                or wrapper_payload["workflow_run_id"] != str(export.run_id)
                or wrapper_payload["fencing_sequence"] != export.run_id
                or wrapper_payload["fencing_token_sha256"] != manifest["fencing_token_sha256"]
                or wrapper_payload["resume_from"] != manifest["resume_from"]
                or wrapper_payload["child_checkpoint_kind"] != ("official_stress_v2" if binding["stage"] == "L4" else "c6_diagnostic_shard_v2")
                or wrapper_payload["child_checkpoint_path"] != "child-checkpoint.bin"
                or wrapper_payload["child_checkpoint_byte_size"] != len(child)
                or wrapper_payload["child_checkpoint_full_byte_sha256"]
                != hashlib.sha256(child).hexdigest()
                or wrapper_payload["item_manifest_count"] != item_contract["count"]
                or wrapper_payload["item_manifest_sha256"] != item_contract["sha256"]
                or not isinstance(completed_ids, list)
                or completed_ids != child_completed
                or completed_ids != list(item_ids[: len(completed_ids)])
                or wrapper_payload["next_item_ordinal"] != len(completed_ids)
                or wrapper_payload["completed_item_ids_sha256"]
                != hashlib.sha256("".join(f"{item}\n" for item in completed_ids).encode()).hexdigest()
            ):
                _raise("prior checkpoint wrapper identity/progress is invalid")
            previous = hashlib.sha256(wrapper).hexdigest()
        latest = prior[-1][1]
        if resume_from != previous or resume_workflow_run_id != str(latest.run_id):
            _raise("resume does not name the latest checkpoint")
        if checkpoint_path.exists() or lease_path.exists():
            _raise("durable restore target already exists")
        _atomic_bytes(checkpoint_path, latest.files["child-checkpoint.bin"], replace=False)
        old = latest.manifest
        _atomic_bytes(lease_path, canonical_json_bytes({
            "schema_version": 1, "logical_run_id": binding["logical_run_id"],
            "attempt_id": old["attempt_id"],
            "fencing_token_sha256": old["fencing_token_sha256"],
            "fencing_sequence": latest.run_id, "active": False, "terminal": True,
            "resume_from": old["resume_from"], "checkpoint_id": previous,
        }), replace=False)

    def producer(
        self,
        identity: Mapping[str, Any],
        *,
        expected_record: str,
        expected_logical_run: str,
        workflow: Mapping[str, Any],
        run_bindings_revision: str,
        destination: Path,
    ) -> RemoteExport:
        """Download, validate, and materialize one exact read-only producer."""
        keys = {
            "artifact_full_byte_sha256", "attempt_id", "binding_id",
            "logical_run_id", "workflow_run_id",
        }
        try:
            require_exact_keys(identity, keys, label="producer identity")
        except ContractError as exc:
            _raise(str(exc), exc)
        run_text = identity["workflow_run_id"]
        if not isinstance(run_text, str) or not run_text.isdigit():
            _raise("producer workflow_run_id is invalid")
        run_id = int(run_text)
        detail = self._json(
            f"{self.api_url}/repos/{self.repository}/actions/runs/{run_id}",
            "producer run",
        )
        if (
            detail.get("event") != "workflow_dispatch"
            or detail.get("head_branch") != workflow["dispatch_ref"]
            or detail.get("head_sha") != workflow["revision"]
            or detail.get("run_attempt") != 1
            or detail.get("status") != "completed"
        ):
            _raise("producer workflow identity is invalid")
        artifacts = self._pages(f"actions/runs/{run_id}/artifacts", "artifacts")
        expected_name = f"c6-bound-{expected_logical_run}-{identity['attempt_id']}"
        matches = [item for item in artifacts if item.get("name") == expected_name]
        if len(matches) != 1:
            _raise("producer has no unique exact artifact")
        export = self._export(run_id, matches[0])
        manifest = export.manifest
        expected = {
            "kind": "result", "binding_id": identity["binding_id"],
            "logical_run_id": expected_logical_run,
            "attempt_id": identity["attempt_id"], "workflow_run_id": run_text,
            "run_bindings_revision": run_bindings_revision,
            "workflow_revision": workflow["revision"],
        }
        if identity["binding_id"] != expected_record or any(
            manifest[key] != value for key, value in expected.items()
        ):
            _raise("producer manifest identity mismatch")
        payload_names = set(export.files) - {"digest.json"}
        if len(payload_names) != 1:
            _raise("producer result file set is invalid")
        payload_name = payload_names.pop()
        if hashlib.sha256(export.files[payload_name]).hexdigest() != identity[
            "artifact_full_byte_sha256"
        ]:
            _raise("producer artifact full-byte SHA-256 mismatch")
        runner_temp = Path(os.environ.get("RUNNER_TEMP", "")).resolve()
        resolved = destination.resolve()
        if not runner_temp.is_dir() or not resolved.is_relative_to(runner_temp):
            _raise("producer destination is outside RUNNER_TEMP")
        if destination.exists():
            _raise("producer destination already exists")
        destination.mkdir(parents=True, mode=0o700)
        for name, data in {"manifest.json": export.manifest_bytes, **export.files}.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_bytes(target, data, replace=False)
            target.chmod(0o400)
        return export


def resolve_attempt_paths(
    binding: Mapping[str, Any], attempt_id: str
) -> dict[str, Path]:
    """Derive every writable v2 path from the signed logical/attempt IDs."""
    logical_run_id = binding.get("logical_run_id")
    stage = binding.get("stage")
    if not isinstance(logical_run_id, str):
        _raise("invalid logical_run_id")
    _validate_id(logical_run_id, "logical_run_id")
    _validate_id(attempt_id, "attempt_id")
    if stage not in {"L1", "QUALIFICATION", "L2", "L4"}:
        _raise("binding stage is invalid")
    root = Path("artifacts/checkpoints/c6") / logical_run_id / "attempts" / attempt_id
    return {
        "root": root,
        "payload": root / ("official-artifact.json" if stage == "L4" else "payload.json"),
        "digest": root / "digest.json",
        "checkpoint": root / "checkpoint.json",
        "child_checkpoint": root / "child-checkpoint.bin",
        "lease": root.parent.parent / "lease.json",
    }


def execution_item_ids(
    binding: Mapping[str, Any], preregistration: Mapping[str, Any]
) -> list[str]:
    """Mechanically rebuild every static v2 item manifest from frozen P."""
    record_id = binding["record_id"]
    manifests = preregistration["scenario_manifests"]
    if record_id == "c6.s.qualification":
        _raise("qualification item manifest requires its validated producer")
    if record_id == "c6.base.l1":
        scenario_path = Path(manifests["L1_ECONOMIC_SCENARIO_IDS"]["path"])
        scenarios = scenario_path.read_text(encoding="utf-8").splitlines()
        variants = manifests["L1_BASE_EVALUATION_MANIFEST"]["core_variant_order"]
        evaluations = [f"{variant}::{scenario}" for variant in variants for scenario in scenarios]
        evaluations += [
            f"{variant}::add-one-13-601869"
            for variant in manifests["L1_BASE_EVALUATION_MANIFEST"]["causal_intervention_order"]
        ]
        controls = manifests["L1_BASE_SYNTHETIC_CONTROL_IDS"]["ids"]
    elif record_id == "c6.base_plus_s.l1":
        scenario_path = Path(manifests["L1_ECONOMIC_SCENARIO_IDS"]["path"])
        scenarios = scenario_path.read_text(encoding="utf-8").splitlines()
        evaluations = [f"C6-Base+S::{scenario}" for scenario in scenarios]
        controls = manifests["L1_S_SYNTHETIC_CONTROL_IDS"]["ids"]
    elif binding["stage"] == "L2":
        return [f"scenario/{item}" for item in manifests["L2_EXACT_SCENARIO_IDS"]["ids"]]
    else:
        from quantfusion.application import stress_scenarios

        plan = stress_scenarios._multi_seed_scenarios(
            random_samples=50,
            permutation_samples=50,
            seeds=(20260807, 20260817, 20260827),
        )
        return [f"scenario/{item['scenario_id']}" for item in plan]
    no_drift = manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]["ids"]
    return (
        [f"evaluation/{item}" for item in evaluations]
        + [f"control/{item}" for item in controls]
        + [f"no-drift/{item}" for item in no_drift]
    )


def qualification_item_ids(export: RemoteExport) -> list[str]:
    """Derive the frozen residual set only from a validated Base producer."""
    raw = export.files.get("payload.json")
    payload = strict_json_loads(raw) if raw is not None else None
    if not isinstance(payload, dict) or not isinstance(payload.get("evaluations"), list):
        _raise("Base producer has no valid evaluation array")
    selected = [
        item for item in payload["evaluations"]
        if isinstance(item, dict) and item.get("variant_id") == "C6-Base"
    ]
    scenario_ids = [str(item.get("scenario_id", "")) for item in selected]
    if len(selected) != 765 or len(set(scenario_ids)) != 765 or any(not item for item in scenario_ids):
        _raise("Base producer C6-Base slice identity is invalid")
    residuals = sorted(
        item["scenario_id"] for item in selected
        if abs(float(item["official_metrics"]["max_drawdown"])) > 0.18 + 1e-15
    )
    if not residuals:
        _raise("empty residual set forbids S qualification")
    return [f"qualification/{item}" for item in residuals]


def checkpoint_progress(
    child_bytes: bytes,
    *,
    stage: str,
    binding_signature: str,
    item_ids: Sequence[str],
) -> list[str]:
    """Validate a child checkpoint and return its exact completed item prefix."""
    try:
        payload = strict_json_loads(child_bytes)
    except ContractError as exc:
        _raise("child checkpoint is invalid JSON", exc)
    if not isinstance(payload, dict):
        _raise("child checkpoint must be an object")
    if stage == "L4":
        results = payload.get("results")
        if set(payload) != {"signature", "provenance", "completed", "scenario_count", "results"}:
            _raise("official child checkpoint has the wrong schema")
        if not isinstance(results, list) or payload["completed"] != len(results):
            _raise("official child checkpoint progress is invalid")
        completed = [f"scenario/{item.get('scenario_id')}" for item in results if isinstance(item, dict)]
    else:
        expected = {
            "schema_version", "kind", "binding_signature", "item_manifest_count",
            "item_manifest_sha256", "completed_count", "completed_items",
        }
        try:
            require_exact_keys(payload, expected, label="diagnostic child checkpoint")
        except ContractError as exc:
            _raise(str(exc), exc)
        items = payload["completed_items"]
        if (
            payload["schema_version"] != 2
            or payload["kind"] != "c6_diagnostic_shard_v2"
            or payload["binding_signature"] != binding_signature
            or not isinstance(items, list)
            or payload["completed_count"] != len(items)
        ):
            _raise("diagnostic child checkpoint identity/progress is invalid")
        completed = [str(item.get("item_id", "")) for item in items if isinstance(item, dict)]
    if not completed or completed != list(item_ids[: len(completed)]):
        _raise("child checkpoint does not contain an exact nonempty prefix")
    return completed


def runtime_binding_signature(payload: Mapping[str, Any]) -> str:
    """Hash the exact runtime values that cannot be frozen into R itself."""
    required = {
        "record_signature", "run_bindings_revision", "attempt_id",
        "workflow_run_id", "resume_from", "resume_workflow_run_id",
        "fencing_token_sha256", "direct_producer_identity",
        "transitive_base_producer_identity", "D", "C", "selection_status",
        "item_manifest_count", "item_manifest_sha256",
    }
    try:
        require_exact_keys(payload, required, label="runtime binding signature")
    except ContractError as exc:
        _raise(str(exc), exc)
    return canonical_payload_hash(payload)


def build_digest(
    *,
    stage: str,
    record_id: str,
    binding_signature: str,
    p_identity: Mapping[str, Any],
    r_revision: str,
    source_revision: str,
    source_tree: str,
    d_identity: Mapping[str, Any] | None,
    implementation: Mapping[str, Any] | None,
    artifact_path: str,
    artifact_bytes: bytes,
    payload_schema: Mapping[str, Any],
    payload: Mapping[str, Any],
    exit_code: int,
) -> dict[str, Any]:
    """Build the exact v2 digest sidecar without modifying official bytes."""
    if stage not in {"L1", "QUALIFICATION", "L2", "L4"}:
        _raise("digest stage is invalid")
    if exit_code not in ({0, 2} if stage == "L4" else {0}):
        _raise("digest exit code violates the stage contract")
    return {
        "schema_version": 2,
        "kind": "c6_artifact_digest",
        "stage": stage,
        "record_id": record_id,
        "binding_signature": binding_signature,
        "P": dict(p_identity),
        "R_revision": r_revision,
        "source_revision": source_revision,
        "source_tree": source_tree,
        "D": None if d_identity is None else dict(d_identity),
        "C": None if implementation is None else dict(implementation),
        "selection_status": "unselected" if d_identity is None else "selected",
        "artifact_path": artifact_path,
        "artifact_byte_size": len(artifact_bytes),
        "artifact_full_byte_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "canonical_result_payload_schema": dict(payload_schema),
        "canonical_result_payload_sha256": canonical_payload_hash(payload),
        "envelope_path": "manifest.json",
        "official_artifact_unchanged": stage == "L4",
        "exit_code": exit_code,
    }


def validate_result_payload(
    artifact: Mapping[str, Any], binding: Mapping[str, Any], prereg: Mapping[str, Any]
) -> None:
    """Reject missing/extra v2 payload fields before hashing or publication."""
    schema = binding["canonical_payload_schema"]
    name = schema.get("name") if isinstance(schema, dict) else None
    definitions = dict(prereg["schema_catalog"]["definitions"])
    qualification = prereg.get("S_QUALIFICATION_RUN")
    if isinstance(qualification, Mapping):
        definitions["S_QUALIFICATION_RUN.output_schema"] = qualification["output_schema"]
        definitions["per_residual"] = qualification["per_residual_schema"]
        definitions["criterion_result"] = qualification["criterion_result_schema"]
    if not isinstance(name, str) or name not in definitions:
        _raise("R canonical payload schema is unknown")
    try:
        def validate_named(value: object, definition_name: str, label: str) -> None:
            definition = definitions[definition_name]
            expected = definition.get("exact_keys")
            if not isinstance(value, Mapping):
                _raise(f"{label} must be an object")
            if definition_name == "official_L4_payload":
                variant = "accepted_initial_additional_exact_keys" if value.get("acceptance_status") == "accepted" else "rejected_additional_exact_keys"
                expected = definition["common_exact_keys"] + definition[variant]
            require_exact_keys(value, expected, label=label)
            for field, descriptor in definition.get("field_types", {}).items():
                child = str(descriptor).split(" ", 1)[0]
                if definition_name == "per_residual" and field == "criteria":
                    child = "criterion_result"
                if child not in definitions:
                    continue
                nested = value[field]
                if nested is None and "or null" in descriptor:
                    continue
                if "array" in descriptor:
                    if not isinstance(nested, list):
                        _raise(f"{label}.{field} must be an array")
                    for index, item in enumerate(nested):
                        validate_named(item, child, f"{label}.{field}[{index}]")
                else:
                    validate_named(nested, child, f"{label}.{field}")
        validate_named(artifact, name, name)
    except (ContractError, KeyError) as exc:
        _raise(f"result payload schema mismatch: {exc}", exc)


def child_environment(
    environment: Mapping[str, str],
    *,
    checkpoint_path: Path | None,
    signature: str,
    logical_run_id: str,
    attempt_id: str,
    fencing_token: str,
    resume_from: str,
) -> dict[str, str]:
    """Expose only P's process allowlist and exact child checkpoint contract."""
    allowed = ("PATH", "LANG", "LC_ALL", "TZ", "PYTHONHASHSEED")
    result = {key: environment[key] for key in allowed if key in environment}
    result.update(
        {
            "C6_BOUND_CHECKPOINT_PATH": (
                "" if checkpoint_path is None else checkpoint_path.as_posix()
            ),
            "C6_BOUND_SIGNATURE": signature,
            "C6_BOUND_LOGICAL_RUN_ID": logical_run_id,
            "C6_BOUND_ATTEMPT_ID": attempt_id,
            "C6_BOUND_FENCING_TOKEN": fencing_token,
            "C6_BOUND_RESUME_FROM": resume_from,
        }
    )
    return result


def validate_runtime_identity(
    binding: Mapping[str, Any],
    *,
    source_revision: str,
    workflow_revision: str,
    logical_run_id: str,
    attempt_id: str,
    fencing_token: str,
    resume_from: str,
    runner_image_os: str,
    runner_image_version: str,
    python_version: str,
    repository: str,
    workflow_run_attempt: int,
) -> None:
    """Match workflow inputs to one exact R binding and its attempt policy."""
    if binding["source_revision"] != source_revision:
        _raise("runtime source_revision does not match R")
    workflow = binding["workflow"]
    if workflow["revision"] != workflow_revision:
        _raise("runtime workflow_revision does not match R")
    runtime = binding["runtime"]
    for key, value in (
        ("runner_image_os", runner_image_os),
        ("runner_image_version", runner_image_version),
        ("python_version", python_version),
    ):
        if runtime[key] != value:
            _raise(f"runtime {key} does not match R")
    if repository != "ychenracing/trade":
        _raise("runtime repository does not match P")
    if binding["logical_run_id"] != logical_run_id:
        _raise("runtime logical_run_id does not match R")
    _validate_id(attempt_id, "attempt_id")
    _validate_token(fencing_token)
    if workflow_run_attempt != 1:
        _raise("native workflow reruns are forbidden")
    initial = attempt_id == binding["initial_attempt_id"]
    if initial != (resume_from == ""):
        _raise("attempt and resume state disagree")
    if not initial and re.fullmatch(r"r[1-9][0-9]*-[0-9a-f]{12}", attempt_id) is None:
        _raise("resume attempt_id violates P")
    if resume_from and _SHA256.fullmatch(resume_from) is None:
        _raise("resume_from violates P")


def _git_text(arguments: list[str], *, cwd: Path) -> str:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        _raise("cannot verify frozen Git checkout", exc)


def _verify_checkout(
    *,
    source_revision: str,
    source_tree: str,
    run_bindings_revision: str,
    bindings_file: Path,
) -> None:
    source_root = Path.cwd()
    if _git_text(["rev-parse", "HEAD"], cwd=source_root) != source_revision:
        _raise("source checkout HEAD does not match runtime source_revision")
    if _git_text(["rev-parse", "HEAD^{tree}"], cwd=source_root) != source_tree:
        _raise("source checkout tree does not match R")
    if _git_text(["status", "--porcelain", "-uall"], cwd=source_root):
        _raise("source checkout is not clean before bound execution")
    if _git_text(["rev-parse", "HEAD"], cwd=bindings_file.parent) != run_bindings_revision:
        _raise("bindings checkout HEAD does not match run_bindings_revision")
    if _git_text(["status", "--porcelain", "-uall"], cwd=bindings_file.parent):
        _raise("bindings checkout is not clean")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute one immutable C6 R binding")
    parser.add_argument("--bindings-file", required=True, type=Path)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--logical-run-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--resume-from", default="")
    parser.add_argument("--resume-workflow-run-id", default="")
    parser.add_argument("--d-commit", default="")
    parser.add_argument("--d-selection-blob-oid", default="")
    parser.add_argument("--d-selection-file-sha256", default="")
    parser.add_argument("--decision-checkout", required=True, type=Path)
    parser.add_argument("--producer-identity-json", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--run-bindings-revision", required=True)
    parser.add_argument("--workflow-revision", required=True)
    parser.add_argument("--runner-image-os", required=True)
    parser.add_argument("--runner-image-version", required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-attempt", required=True, type=int)
    parser.add_argument("--require-durable-lease", action="store_true")
    return parser


def _durable_lease_precondition(args: argparse.Namespace) -> None:
    if not args.require_durable_lease:
        return
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_RUN_ID") != args.workflow_run_id
        or os.environ.get("GITHUB_RUN_ATTEMPT") != str(args.workflow_run_attempt)
        or not os.environ.get("GITHUB_TOKEN")
    ):
        _raise("durable GitHub Actions lease precondition is not satisfied")


def execute_bound_binding(args: argparse.Namespace) -> int:
    """Validate, execute, and atomically export one exact R-bound command."""
    fencing_token = os.environ.get("C6_FENCING_TOKEN", "")
    _validate_token(fencing_token)
    try:
        producer_identity = strict_json_loads(args.producer_identity_json)
    except ContractError as exc:
        _raise(f"producer identity is invalid: {exc}", exc)
    if not isinstance(producer_identity, dict):
        _raise("producer identity must be an object")
    attempt_identity = {
        "candidate_id": args.candidate_id,
        "repository": args.repository,
        "workflow_run_id": args.workflow_run_id,
        "workflow_run_attempt": str(args.workflow_run_attempt),
        "resume_from": args.resume_from,
        "resume_workflow_run_id": args.resume_workflow_run_id,
        "d_commit": args.d_commit,
        "d_selection_blob_oid": args.d_selection_blob_oid,
        "d_selection_file_sha256": args.d_selection_file_sha256,
        "producer_identity": producer_identity,
        "runner_image_os": args.runner_image_os,
        "runner_image_version": args.runner_image_version,
        "python_version": args.python_version,
    }
    prereg_path = Path("artifacts/diagnostics/c6-preregistration.json")
    try:
        prereg = load_preregistration(prereg_path, repository=Path.cwd())
        run_bindings = load_run_bindings(args.bindings_file)
        binding = select_binding(
            run_bindings, args.binding_id, candidate_id=args.candidate_id
        )
    except ContractError as exc:
        _raise(f"immutable input validation failed: {exc}", exc)

    p_identity = run_bindings["P"]
    if file_sha256(prereg_path) != p_identity["sha256"]:
        _raise("source P full-byte SHA-256 does not match R")
    if (
        _git_text(["rev-parse", f"{p_identity['commit']}^{{tree}}"], cwd=Path.cwd())
        != p_identity["tree"]
    ):
        _raise("P commit tree does not match R")
    p_record = _git_text(
        ["ls-tree", p_identity["commit"], "--", prereg_path.as_posix()],
        cwd=Path.cwd(),
    ).split()
    if len(p_record) != 4 or p_record[2] != p_identity["blob"]:
        _raise("P commit does not bind the expected preregistration blob")
    if _git_text(["hash-object", str(prereg_path)], cwd=Path.cwd()) != p_identity["blob"]:
        _raise("source P blob does not match R")

    validate_runtime_identity(
        binding,
        source_revision=args.source_revision,
        workflow_revision=args.workflow_revision,
        logical_run_id=args.logical_run_id,
        attempt_id=args.attempt_id,
        fencing_token=fencing_token,
        resume_from=args.resume_from,
        runner_image_os=args.runner_image_os,
        runner_image_version=args.runner_image_version,
        python_version=args.python_version,
        repository=args.repository,
        workflow_run_attempt=args.workflow_run_attempt,
    )
    if platform.python_version() != args.python_version:
        _raise("active Python patch version does not match R")
    _verify_checkout(
        source_revision=args.source_revision,
        source_tree=binding["source_tree"],
        run_bindings_revision=args.run_bindings_revision,
        bindings_file=args.bindings_file,
    )
    _durable_lease_precondition(args)
    if not args.workflow_run_id.isdigit() or args.workflow_run_attempt < 1:
        _raise("workflow run identity is invalid")

    if args.candidate_id != binding["candidate_id"]:
        _raise("runtime candidate_id does not match R")
    paths = resolve_attempt_paths(binding, args.attempt_id)
    output_path = paths["payload"]
    lease_path = paths["lease"]
    checkpoint_path = paths["child_checkpoint"]
    if output_path.exists():
        _raise("bound output path already exists")
    store = (
        GitHubActionsLeaseStore(
            args.repository,
            os.environ["GITHUB_TOKEN"],
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
        if args.require_durable_lease else None
    )

    item_contract = binding["item_manifest_contract"]
    empty_producer = {
        "artifact_full_byte_sha256": "", "attempt_id": "", "binding_id": "",
        "logical_run_id": "", "workflow_run_id": "",
    }
    producer_map = {
        "c6.s.qualification": ("c6.base.l1", "c6-v6-base-l1"),
        "c6.base_plus_s.l1": ("c6.s.qualification", "c6-v6-s-qualification"),
        "c6.base.selected.l4": ("c6.selected.l2", "c6-v6-base-l2"),
        "c6.base_plus_s.selected.l4": (
            "c6.selected.l2", "c6-v6-base-plus-s-l2"
        ),
    }
    direct_export = None
    direct_root = None
    expected_producer = producer_map.get(binding["record_id"])
    if expected_producer is None:
        if producer_identity != empty_producer:
            _raise("binding requires the empty producer identity")
    else:
        if store is None or producer_identity == empty_producer:
            _raise("binding requires one durable producer")
        direct_root = Path(os.environ["RUNNER_TEMP"]) / "c6-producers" / str(
            producer_identity["binding_id"]
        ) / str(producer_identity["workflow_run_id"]) / str(
            producer_identity["attempt_id"]
        )
        direct_export = store.producer(
            producer_identity,
            expected_record=expected_producer[0],
            expected_logical_run=expected_producer[1],
            workflow=binding["workflow"],
            run_bindings_revision=args.run_bindings_revision,
            destination=direct_root,
        )
    transitive_identity: Mapping[str, Any] = empty_producer
    base_root = None
    if binding["record_id"] == "c6.base_plus_s.l1":
        if direct_export is None or store is None:
            _raise("Base+S binding requires its qualification producer")
        qualification = strict_json_loads(direct_export.files["payload.json"])
        if not isinstance(qualification, dict) or not isinstance(
            qualification.get("base_producer_identity"), dict
        ):
            _raise("qualification producer lacks its Base producer identity")
        transitive_identity = qualification["base_producer_identity"]
        base_root = Path(os.environ["RUNNER_TEMP"]) / "c6-producers" / "c6.base.l1" / str(
            transitive_identity["workflow_run_id"]
        ) / str(transitive_identity["attempt_id"])
        store.producer(
            transitive_identity,
            expected_record="c6.base.l1",
            expected_logical_run="c6-v6-base-l1",
            workflow=binding["workflow"],
            run_bindings_revision=args.run_bindings_revision,
            destination=base_root,
        )
    item_ids = (
        qualification_item_ids(direct_export)
        if binding["record_id"] == "c6.s.qualification" and direct_export is not None
        else execution_item_ids(binding, prereg)
    )
    item_count = len(item_ids)
    item_hash = hashlib.sha256(
        "".join(f"{item}\n" for item in item_ids).encode()
    ).hexdigest()
    if type(item_count) is not int or item_count < 1 or not isinstance(
        item_hash, str
    ) or _SHA256.fullmatch(item_hash) is None or (
        item_count, item_hash
    ) != (item_contract.get("count"), item_contract.get("sha256")) and binding[
        "record_id"
    ] != "c6.s.qualification":
        _raise("binding item manifest is unresolved")
    if store is not None:
        store.restore(
            binding=binding, current_run_id=int(args.workflow_run_id),
            resume_from=args.resume_from,
            resume_workflow_run_id=args.resume_workflow_run_id,
            checkpoint_path=checkpoint_path, lease_path=lease_path,
            run_bindings_revision=args.run_bindings_revision, item_ids=item_ids,
        )
    selected = binding["stage"] in {"L2", "L4"}
    d_identity = None
    implementation = None
    if selected:
        if not all((args.d_commit, args.d_selection_blob_oid, args.d_selection_file_sha256)):
            _raise("selected binding requires the complete D identity")
        d_identity = {
            "commit": args.d_commit,
            "selection_blob_oid": args.d_selection_blob_oid,
            "selection_file_sha256": args.d_selection_file_sha256,
        }
        selection_path = args.decision_checkout / "artifacts/diagnostics/c6-selection.json"
        implementation = validate_selection_commit(
            selection_path,
            run_bindings=run_bindings,
            run_bindings_revision=args.run_bindings_revision,
            candidate_id=args.candidate_id,
        )
        if file_sha256(selection_path) != args.d_selection_file_sha256:
            _raise("D selection file SHA-256 mismatch")
    elif any((args.d_commit, args.d_selection_blob_oid, args.d_selection_file_sha256)):
        _raise("pre-selection binding forbids D")
    signature_payload = {
        "record_signature": binding["record_signature"],
        "run_bindings_revision": args.run_bindings_revision,
        "attempt_id": args.attempt_id,
        "workflow_run_id": args.workflow_run_id,
        "resume_from": args.resume_from,
        "resume_workflow_run_id": args.resume_workflow_run_id,
        "fencing_token_sha256": hashlib.sha256(fencing_token.encode()).hexdigest(),
        "direct_producer_identity": producer_identity,
        "transitive_base_producer_identity": transitive_identity,
        "D": d_identity,
        "C": implementation,
        "selection_status": "selected" if selected else "unselected",
        "item_manifest_count": item_count,
        "item_manifest_sha256": item_hash,
    }
    binding_signature = runtime_binding_signature(signature_payload)

    lease = ExclusiveLease.acquire(
        lease_path,
        logical_run_id=args.logical_run_id,
        attempt_id=args.attempt_id,
        fencing_token=fencing_token,
        fencing_sequence=int(args.workflow_run_id),
        resume_from=args.resume_from,
    )
    late_values = {
        "RUN_BINDINGS_PATH": str(args.bindings_file.resolve()),
        "OUTPUT_PATH": output_path.as_posix(),
        "CHILD_CHECKPOINT_PATH": checkpoint_path.as_posix(),
    }
    if direct_root is not None:
        late_values.update({
            "PRODUCER_EXPORT_PATH": str(direct_root.resolve()),
            "PRODUCER_ARTIFACT_SHA256": str(
                producer_identity["artifact_full_byte_sha256"]
            ),
        })
    if base_root is not None:
        late_values.update({
            "BASE_PRODUCER_EXPORT_PATH": str(base_root.resolve()),
            "BASE_PRODUCER_ARTIFACT_SHA256": str(
                transitive_identity["artifact_full_byte_sha256"]
            ),
        })
    declared = binding["runtime_late_slots"]
    if any(slot not in late_values for slot in declared):
        _raise("binding requires unresolved producer paths")
    try:
        argv = render_bound_argv(
            binding["argv"],
            {key: late_values[key] for key in declared},
            allowed_slots=declared,
        )
    except (ContractError, KeyError) as exc:
        _raise(f"cannot construct exact bound argv: {exc}", exc)
    if argv[:3] not in (
        ["python", "-m", "quantfusion.application.c6_diagnostics"],
        ["python", "-m", "quantfusion.application.c6_s_qualification"],
        ["python", "-m", "quantfusion.application.stress"],
    ):
        _raise("R argv does not name an authorized C6 entrypoint")
    _verify_checkout(
        source_revision=args.source_revision,
        source_tree=binding["source_tree"],
        run_bindings_revision=args.run_bindings_revision,
        bindings_file=args.bindings_file,
    )

    environment = child_environment(
        os.environ,
        checkpoint_path=checkpoint_path,
        signature=binding_signature,
        logical_run_id=args.logical_run_id,
        attempt_id=args.attempt_id,
        fencing_token=fencing_token,
        resume_from=args.resume_from,
    )
    completed = subprocess.run(argv, check=False, env=environment)
    export_root = Path("artifacts/checkpoints/c6/sealed-export")
    valid_result_codes = set(binding["exit_semantics"]["terminal_success_exit_codes"])
    valid_result_codes.update(binding["exit_semantics"]["terminal_rejected_exit_codes"])
    checkpoint_code = binding["exit_semantics"]["checkpoint_incomplete_exit_code"]
    if completed.returncode == checkpoint_code:
        checkpoint_id = None
        if checkpoint_path.is_file() and not checkpoint_path.is_symlink():
            child_bytes = checkpoint_path.read_bytes()
            completed_ids = checkpoint_progress(
                child_bytes,
                stage=binding["stage"],
                binding_signature=binding_signature,
                item_ids=item_ids,
            )
            wrapper = {
                "schema_version": 2, "kind": "c6_bound_checkpoint",
                "status": "checkpointed_incomplete", "record_id": binding["record_id"],
                "binding_signature": binding_signature,
                "source_revision": args.source_revision,
                "logical_run_id": args.logical_run_id, "attempt_id": args.attempt_id,
                "workflow_run_id": args.workflow_run_id,
                "fencing_sequence": int(args.workflow_run_id),
                "fencing_token_sha256": signature_payload["fencing_token_sha256"],
                "resume_from": args.resume_from,
                "resume_workflow_run_id": args.resume_workflow_run_id,
                "child_checkpoint_kind": (
                    "official_stress_v2" if binding["stage"] == "L4"
                    else "c6_diagnostic_shard_v2"
                ),
                "child_checkpoint_path": "child-checkpoint.bin",
                "child_checkpoint_byte_size": len(child_bytes),
                "child_checkpoint_full_byte_sha256": hashlib.sha256(child_bytes).hexdigest(),
                "item_manifest_count": item_count,
                "item_manifest_sha256": item_hash,
                "completed_item_ids": completed_ids,
                "completed_item_ids_sha256": hashlib.sha256(
                    "".join(f"{item}\n" for item in completed_ids).encode()
                ).hexdigest(),
                "next_item_ordinal": len(completed_ids),
                "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            wrapper_bytes = canonical_json_bytes(wrapper)
            checkpoint_id = hashlib.sha256(wrapper_bytes).hexdigest()
            seal_export(
                export_root,
                kind="checkpoint",
                source_revision=args.source_revision,
                run_bindings_revision=args.run_bindings_revision,
                workflow_revision=args.workflow_revision,
                binding_id=args.binding_id,
                logical_run_id=args.logical_run_id,
                attempt_id=args.attempt_id,
                fencing_token=fencing_token,
                attempt_identity=attempt_identity,
                files={"checkpoint.json": wrapper_bytes, "child-checkpoint.bin": child_bytes},
            )
        lease.release(terminal=True, checkpoint_id=checkpoint_id)
        return completed.returncode
    if completed.returncode not in valid_result_codes:
        lease.release(terminal=True)
        return completed.returncode

    lease.assert_current()
    if not output_path.is_file() or output_path.is_symlink():
        _raise("successful bound command did not produce a regular output file")
    try:
        artifact = strict_json_load(output_path)
    except ContractError as exc:
        _raise(f"bound output is not strict JSON: {exc}", exc)
    if not isinstance(artifact, dict):
        _raise("bound output root must be an object")
    validate_result_payload(artifact, binding, prereg)
    payload = artifact
    artifact_bytes = output_path.read_bytes()
    artifact_name = "official-artifact.json" if binding["stage"] == "L4" else "payload.json"
    sidecar = build_digest(
        stage=binding["stage"], record_id=binding["record_id"],
        binding_signature=binding_signature, p_identity=p_identity,
        r_revision=args.run_bindings_revision, source_revision=args.source_revision,
        source_tree=binding["source_tree"], d_identity=d_identity,
        implementation=implementation, artifact_path=artifact_name,
        artifact_bytes=artifact_bytes, payload_schema=binding["canonical_payload_schema"],
        payload=payload, exit_code=completed.returncode,
    )
    seal_export(
        export_root,
        kind="result",
        source_revision=args.source_revision,
        run_bindings_revision=args.run_bindings_revision,
        workflow_revision=args.workflow_revision,
        binding_id=args.binding_id,
        logical_run_id=args.logical_run_id,
        attempt_id=args.attempt_id,
        fencing_token=fencing_token,
        attempt_identity=attempt_identity,
        files={
            artifact_name: artifact_bytes,
            "digest.json": canonical_json_bytes(sidecar),
        },
    )
    lease.release(terminal=True)
    return completed.returncode


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return execute_bound_binding(args)
    except BoundRunError as exc:
        print(f"C6 bound run rejected: {exc}", file=sys.stderr)
        return 2


def _export_relative_path(raw_path: str) -> PurePosixPath:
    pure = PurePosixPath(raw_path)
    if (
        pure.is_absolute()
        or not pure.parts
        or ".." in pure.parts
        or pure.as_posix() != raw_path
        or any(part.startswith(".") for part in pure.parts)
        or raw_path == "manifest.json"
    ):
        _raise(f"invalid export path: {raw_path}")
    return pure


def seal_export(
    export_root: str | Path,
    *,
    kind: str,
    source_revision: str,
    run_bindings_revision: str,
    workflow_revision: str,
    binding_id: str,
    logical_run_id: str,
    attempt_id: str,
    fencing_token: str,
    attempt_identity: Mapping[str, Any],
    files: Mapping[str, bytes],
) -> dict[str, Any]:
    """Publish one immutable directory matching the workflow's exact schema."""
    root = Path(export_root)
    if root.exists():
        _raise(f"sealed export already exists: {root}")
    if kind not in {"checkpoint", "result"}:
        _raise("sealed export kind must be checkpoint or result")
    for revision, label in (
        (source_revision, "source_revision"),
        (run_bindings_revision, "run_bindings_revision"),
        (workflow_revision, "workflow_revision"),
    ):
        if not isinstance(revision, str) or _SHA.fullmatch(revision) is None:
            _raise(f"invalid {label}")
    _validate_id(binding_id, "binding_id")
    _validate_id(logical_run_id, "logical_run_id")
    _validate_id(attempt_id, "attempt_id")
    _validate_token(fencing_token)
    if not files:
        _raise("sealed export file map cannot be empty")

    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=root.parent, prefix=f".{root.name}-"))
    try:
        hashes: dict[str, str] = {}
        for relative, content in files.items():
            pure = _export_relative_path(relative)
            if not isinstance(content, bytes):
                _raise(f"export content must be bytes: {relative}")
            target = staging.joinpath(*pure.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_bytes(target, content, replace=False)
            hashes[relative] = hashlib.sha256(content).hexdigest()
        manifest: dict[str, Any] = {
            "schema_version": 2,
            "kind": kind,
            "sealed": True,
            "source_revision": source_revision,
            "run_bindings_revision": run_bindings_revision,
            "workflow_revision": workflow_revision,
            "binding_id": binding_id,
            **dict(attempt_identity),
            "logical_run_id": logical_run_id,
            "attempt_id": attempt_id,
            "fencing_token_sha256": hashlib.sha256(
                fencing_token.encode("utf-8")
            ).hexdigest(),
            "files": dict(sorted(hashes.items())),
        }
        require_exact_keys(manifest, _EXPORT_KEYS, label="sealed export manifest")
        _atomic_bytes(
            staging / "manifest.json", canonical_json_bytes(manifest), replace=False
        )
        try:
            staging.replace(root)
        except FileExistsError as exc:
            _raise(f"sealed export already exists: {root}", exc)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == "__main__":
    raise SystemExit(main())
