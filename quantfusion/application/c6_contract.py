"""Fail-closed helpers for the frozen C6 execution contract.

This module is deliberately independent from the trading engine.  It validates
identities and serializes evidence; it cannot evaluate a scenario.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import string
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Collection, Mapping, Sequence


class ContractError(ValueError):
    """A frozen C6 identity or schema did not match exactly."""


PREREGISTRATION_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "status",
        "experiment_id",
        "frozen_at",
        "purpose",
        "authority",
        "frozen_file_identity_origin",
        "frozen_file_identities",
        "baseline_compatibility",
        "formal_contract",
        "scenario_manifests",
        "candidate_family",
        "causal_diagnostics",
        "interventions_601869",
        "S",
        "S_QUALIFICATION_RUN",
        "diagnostic_predicate_manifests",
        "FORMAL_GATE_MANIFEST",
        "selection_rule",
        "run_templates",
        "canonical_result_payload_hashing",
        "correctness_contracts",
        "workflow_trigger_matrix",
        "implementation_freeze",
        "economic_tree_and_A",
        "warm_and_data_integrity",
        "claims_not_authorized",
        "stop_conditions",
        "unknowns_before_implementation",
        "transition_reference",
    }
)
PREREGISTRATION_V2_ROOT_KEYS = PREREGISTRATION_ROOT_KEYS | {
    "checkpoint_and_lease_protocol",
    "diagnostic_api_contract",
    "schema_catalog",
}
RUN_BINDINGS_ROOT_KEYS = frozenset(
    {
        "schema_version", "kind", "status", "P", "workflow",
        "implementations", "binding_records", "cross_record_invariants",
        "selection_validator", "serialization",
    }
)
P_IDENTITY_KEYS = frozenset({"commit", "tree", "blob", "sha256"})
RUN_BINDING_KEYS = frozenset(
    {
        "record_id", "workflow_binding_id", "candidate_id", "logical_run_id",
        "initial_attempt_id", "stage", "source_alias", "source_revision",
        "source_tree", "source_blob_identities", "P", "workflow", "entrypoint",
        "argv", "runtime_late_slots", "resolved_inputs",
        "scenario_manifest_identity", "synthetic_control_manifest_identity",
        "evaluation_manifest_identity", "item_manifest_contract",
        "producer_policy", "decision_policy", "runtime", "attempt_policy",
        "paths", "canonical_payload_schema", "exit_semantics", "record_signature",
    }
)
BINDING_RECORD_ORDER = (
    "c6.base.l1",
    "c6.s.qualification",
    "c6.base_plus_s.l1",
    "c6.base.selected.l2",
    "c6.base_plus_s.selected.l2",
    "c6.base.selected.l4",
    "c6.base_plus_s.selected.l4",
)
RUNTIME_LATE_SLOTS = frozenset(
    {
        "RUN_BINDINGS_PATH",
        "OUTPUT_PATH",
        "CHILD_CHECKPOINT_PATH",
        "PRODUCER_EXPORT_PATH",
        "PRODUCER_ARTIFACT_SHA256",
        "BASE_PRODUCER_EXPORT_PATH",
        "BASE_PRODUCER_ARTIFACT_SHA256",
    }
)
RUNTIME_KEYS = frozenset(
    {
        "repository",
        "execution_owner",
        "runner_image_os",
        "runner_image_version",
        "python_version",
    }
)
ATTEMPT_POLICY_KEYS = frozenset(
    {
        "logical_run_id",
        "attempt_id_schema",
        "resume_from_schema",
        "fencing_token_schema",
        "initial_fencing_sequence",
        "duplicate_logical_completion",
    }
)
LEASE_POLICY_KEYS = frozenset(
    {
        "exclusive_per_output_path",
        "monotonic_fencing_token",
        "resume_requires_prior_attempt_terminal_or_inactive",
        "resume_requires_exact_sealed_checkpoint_signature",
    }
)
PATH_KEYS = frozenset({"checkpoint", "output", "lease"})
MANIFEST_IDENTITY_KEYS = frozenset({"name", "count", "unique_count", "sha256"})
SOURCE_BLOB_IDENTITY_KEYS = frozenset({"mode", "git_blob", "sha256"})
CANONICAL_PAYLOAD_CONTRACT_KEYS = frozenset(
    {"version", "artifact_root_exact_keys", "payload_keys"}
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ContractError(f"non-finite JSON constant: {value}")


def _validate_json_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"non-finite JSON number at {path}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"JSON object keys must be strings at {path}")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ContractError(f"unsupported JSON value at {path}: {type(value).__name__}")


def strict_json_loads(content: str | bytes) -> Any:
    """Parse JSON while rejecting duplicates and non-standard numeric values."""
    try:
        payload = json.loads(
            content,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc
    _validate_json_value(payload)
    return payload


def strict_json_load(path: str | Path) -> Any:
    try:
        content = Path(path).read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read JSON file {path}: {exc}") from exc
    return strict_json_loads(content)


def require_exact_keys(
    payload: Mapping[str, Any], expected: Collection[str], *, label: str
) -> None:
    actual = set(payload)
    expected_set = set(expected)
    missing = sorted(expected_set - actual)
    extra = sorted(actual - expected_set)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing keys {missing}")
        if extra:
            details.append(f"unexpected keys {extra}")
        raise ContractError(f"{label} has " + " and ".join(details))


def canonical_json_bytes(payload: Any) -> bytes:
    """Return the P-frozen compact, sorted UTF-8 JSON representation plus LF."""
    _validate_json_value(payload)
    try:
        return (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"payload is not canonical JSON: {exc}") from exc


def canonical_payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def file_sha256(path: str | Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ContractError(f"cannot hash file {path}: {exc}") from exc


def manifest_identity(ids: Sequence[str]) -> dict[str, int | str]:
    """Hash a lexicographically sorted, unique, LF-delimited ID manifest."""
    checked: list[str] = []
    seen: set[str] = set()
    for scenario_id in ids:
        if not isinstance(scenario_id, str) or not scenario_id or "\n" in scenario_id:
            raise ContractError("manifest IDs must be nonempty single-line strings")
        if scenario_id in seen:
            raise ContractError(f"duplicate manifest ID: {scenario_id}")
        checked.append(scenario_id)
        seen.add(scenario_id)
    serialized = "".join(f"{item}\n" for item in sorted(checked)).encode("utf-8")
    return {
        "count": len(checked),
        "unique_count": len(seen),
        "sha256": hashlib.sha256(serialized).hexdigest(),
    }


def _validate_manifest(
    name: str, specification: Mapping[str, Any], ids: Sequence[str]
) -> None:
    identity = manifest_identity(ids)
    for key in ("count", "unique_count"):
        if key in specification and specification[key] != identity[key]:
            raise ContractError(f"{name} {key} does not match its frozen manifest")
    digest_key = (
        "lexicographically_sorted_ids_sha256"
        if "lexicographically_sorted_ids_sha256" in specification
        else "sha256"
    )
    if specification.get(digest_key) != identity["sha256"]:
        raise ContractError(f"{name} SHA-256 does not match its frozen manifest")


def validate_preregistration(
    payload: Mapping[str, Any], *, repository: str | Path | None = None
) -> None:
    """Validate the immutable portions of P without running economic code."""
    schema_version = payload.get("schema_version")
    if schema_version not in {1, 2}:
        raise ContractError("unsupported preregistration schema_version")
    require_exact_keys(
        payload,
        PREREGISTRATION_V2_ROOT_KEYS
        if schema_version == 2
        else PREREGISTRATION_ROOT_KEYS,
        label="preregistration",
    )
    if payload["kind"] != "c6_causal_risk_closure_preregistration":
        raise ContractError("wrong preregistration kind")
    if payload["status"] != "frozen_before_implementation_or_economic_dispatch":
        raise ContractError("preregistration is not frozen")

    manifests = payload["scenario_manifests"]
    if not isinstance(manifests, dict):
        raise ContractError("scenario_manifests must be an object")
    manifest_names = (
        (
            "L1_BASE_SYNTHETIC_CONTROL_IDS",
            "L1_S_SYNTHETIC_CONTROL_IDS",
            "L2_EXACT_SCENARIO_IDS",
        )
        if schema_version == 2
        else ("L1_SYNTHETIC_CONTROL_IDS", "L2_EXACT_SCENARIO_IDS")
    )
    for name in manifest_names:
        specification = manifests.get(name)
        if not isinstance(specification, dict) or not isinstance(
            specification.get("ids"), list
        ):
            raise ContractError(f"{name} must contain an ID array")
        _validate_manifest(name, specification, specification["ids"])

    if schema_version == 2:
        _validate_preregistration_v2(payload)

    if repository is None:
        return
    root = Path(repository)
    authority = payload["authority"]
    if not isinstance(authority, dict):
        raise ContractError("authority must be an object")
    base_revision = authority.get("base_revision")
    base_tree = authority.get("base_tree")
    if not isinstance(base_revision, str) or not _is_sha(base_revision, 40):
        raise ContractError("authority base_revision is invalid")
    if not isinstance(base_tree, str) or not _is_sha(base_tree):
        raise ContractError("authority base_tree is invalid")
    actual_tree = _git_output(
        root, ["rev-parse", "--verify", f"{base_revision}^{{tree}}"]
    ).decode("ascii").strip()
    if actual_tree != base_tree:
        raise ContractError("authority base_tree does not match base_revision")

    identities = payload["frozen_file_identities"]
    if not isinstance(identities, dict) or not identities:
        raise ContractError("frozen_file_identities must be a nonempty object")
    for relative, identity in identities.items():
        if not isinstance(relative, str) or not isinstance(identity, dict):
            raise ContractError("frozen file identity is malformed")
        require_exact_keys(
            identity, {"mode", "git_blob", "sha256"}, label=f"identity {relative}"
        )
        _validate_git_file_identity(root, base_revision, relative, identity)

    dependency = payload["run_templates"].get("dependency_lock")
    if not isinstance(dependency, dict) or not isinstance(dependency.get("path"), str):
        raise ContractError("dependency_lock identity is malformed")
    require_exact_keys(
        dependency,
        {"path", "mode", "git_blob", "sha256", "install"}
        if schema_version == 2
        else {"path", "mode", "git_blob", "sha256"},
        label="dependency_lock",
    )
    _validate_git_file_identity(
        root, base_revision, dependency["path"], dependency, path_is_embedded=True
    )

    l1 = manifests.get("L1_ECONOMIC_SCENARIO_IDS")
    if not isinstance(l1, dict) or not isinstance(l1.get("path"), str):
        raise ContractError("L1_ECONOMIC_SCENARIO_IDS path is missing")
    _safe_repo_path(root, l1["path"])
    raw = _git_output(root, ["show", f"{base_revision}:{l1['path']}"])
    if not raw.endswith(b"\n") or b"\r" in raw:
        raise ContractError("L1 manifest must be LF terminated")
    try:
        l1_ids = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ContractError("L1 manifest must be UTF-8") from exc
    if l1_ids != sorted(l1_ids):
        raise ContractError("L1 manifest must be lexicographically sorted")
    _validate_manifest("L1_ECONOMIC_SCENARIO_IDS", l1, l1_ids)
    if hashlib.sha256(raw).hexdigest() != l1["sha256"]:
        raise ContractError("L1 manifest file SHA-256 mismatch")

    economic = payload["economic_tree_and_A"]
    if not isinstance(economic, dict) or not isinstance(
        economic.get("A_ALLOWLIST"), list
    ):
        raise ContractError("economic tree contract is malformed")
    manifest = economic_tree_manifest(
        base_revision, economic["A_ALLOWLIST"], repository=root
    )
    if len(manifest) != economic.get("base_manifest_entry_count"):
        raise ContractError("base economic tree entry count mismatch")
    if canonical_payload_hash(manifest) != economic.get("base_economic_tree_sha256"):
        raise ContractError("base economic tree SHA-256 mismatch")


def _validate_preregistration_v2(payload: Mapping[str, Any]) -> None:
    """Validate the closed, non-economic v2 schema and execution declarations."""
    authority = payload.get("authority")
    if not isinstance(authority, dict):
        raise ContractError("authority must be an object")
    for key in (
        "pull_request",
        "preregistration_commit",
        "preregistration_tree",
        "preregistration_blob",
        "preregistration_sha256",
    ):
        if authority.get(key) is not None:
            raise ContractError(f"authority {key} must remain null in P")

    catalog = payload.get("schema_catalog")
    if not isinstance(catalog, dict):
        raise ContractError("schema_catalog must be an object")
    require_exact_keys(
        catalog,
        {
            "schema_version",
            "dialect",
            "strict_json",
            "schema_reference_rule",
            "definitions",
        },
        label="schema_catalog",
    )
    if catalog["schema_version"] != 1 or not isinstance(catalog["definitions"], dict):
        raise ContractError("unsupported schema_catalog")
    definitions = catalog["definitions"]
    if not definitions:
        raise ContractError("schema_catalog definitions must be nonempty")
    for name, definition in definitions.items():
        if not isinstance(name, str) or not name or not isinstance(definition, dict):
            raise ContractError("schema_catalog definition is malformed")
        required = {"type", "additional_properties", "field_types", "array_order"}
        if not required.issubset(definition):
            raise ContractError(f"schema {name} is missing closed-schema fields")
        if definition["additional_properties"] is not False:
            raise ContractError(f"schema {name} must reject additional properties")
        key_lists = [
            value
            for key, value in definition.items()
            if key.endswith("exact_keys") and isinstance(value, list)
        ]
        keys = [key for values in key_lists for key in values]
        fields = definition["field_types"]
        order = definition["array_order"]
        if (
            not key_lists
            or len(keys) != len(set(keys))
            or not all(isinstance(key, str) and key for key in keys)
            or not isinstance(fields, dict)
            or set(fields) != set(keys)
            or not isinstance(order, dict)
            or not all(
                key in fields
                or (
                    key.endswith(".*")
                    and key.removesuffix(".*") in fields
                )
                for key in order
            )
        ):
            raise ContractError(f"schema {name} is not exact-key closed")

    predicates = payload.get("diagnostic_predicate_manifests")
    if not isinstance(predicates, dict):
        raise ContractError("diagnostic predicate manifests are malformed")
    grammar = predicates.get("selector_and_path_grammar")
    if not isinstance(grammar, dict) or not isinstance(grammar.get("root_aliases"), dict):
        raise ContractError("diagnostic predicate grammar is malformed")
    aliases = {
        "evaluation": "evaluation_record", "no_drift_pair": "no_drift_pair",
        "synthetic_control": "synthetic_control", "implementation_identity": "implementation_identity",
        "payload.common_prefix_comparisons[]": "common_prefix_comparison",
        "payload.no_effect_comparisons[]": "no_effect_comparison",
        "base_producer.synthetic_control": "synthetic_control", "results[]": "L2_result",
        "results_by_id.<id>": "L2_result", "transition_reference.results_by_id.<id>": "official_result_common",
        "transition_reference.results_by_id.<same_id>": "official_result_common",
        "evaluation_manifest": "manifest_identity", "synthetic_control_manifest": "manifest_identity",
        "no_drift_manifest": "manifest_identity", "scenario_manifest": "manifest_identity",
    }
    allowed_sources = set(grammar.get("selector_sources", ())) | {"literal_ids", "literal_range"}
    for group_name in ("L1_APPLICABLE_DIAGNOSTIC_PREDICATES", "L2_APPLICABLE_DIAGNOSTIC_PREDICATES"):
        group = predicates.get(group_name)
        if not isinstance(group, list) or not group:
            raise ContractError(f"{group_name} must be a nonempty array")
        for criterion in group:
            selector = criterion.get("input_item_ids") if isinstance(criterion, dict) else None
            if not isinstance(selector, dict) or selector.get("source") not in allowed_sources:
                raise ContractError("predicate selector source is unknown")
            paths = criterion.get("input_paths")
            path_values = [item for values in paths.values() for item in values] if isinstance(paths, dict) else paths
            if not isinstance(path_values, list) or not path_values:
                raise ContractError("predicate input_paths must be nonempty")
            for path in path_values:
                alias = next((item for item in sorted(set(grammar["root_aliases"]) | set(aliases), key=len, reverse=True) if path == item or path.startswith(item + ".")), None)
                dynamic_schema = None
                if alias is None and path.startswith("transition_reference.results_by_id."):
                    alias, dynamic_schema = ".".join(path.split(".")[:3]), "official_result_common"
                if alias is None and path.startswith("results_by_id."):
                    alias, dynamic_schema = ".".join(path.split(".")[:2]), "L2_result"
                if alias is None:
                    raise ContractError(f"predicate path has unknown root: {path}")
                remainder = path[len(alias):].removeprefix(".")
                schema_name = dynamic_schema or aliases.get(alias)
                if schema_name is None:
                    root = payload["formal_contract"] if alias == "formal_contract" else {key: None for key in RUN_BINDING_KEYS}
                    if remainder and remainder.split(".", 1)[0] not in root:
                        raise ContractError(f"predicate path does not resolve: {path}")
                    continue
                for segment in remainder.split(".") if remainder else ():
                    segment = segment.removesuffix("[]")
                    fields = definitions[schema_name]["field_types"]
                    if segment not in fields:
                        raise ContractError(f"predicate path does not resolve: {path}")
                    child = str(fields[segment]).split(" ", 1)[0]
                    if child in definitions:
                        schema_name = child

    templates = payload.get("run_templates")
    if not isinstance(templates, dict) or templates.get("schema_version") != 2:
        raise ContractError("run_templates schema is unsupported")
    specs = templates.get("binding_specs")
    if not isinstance(specs, list) or len(specs) != 7:
        raise ContractError("run_templates must contain seven binding specs")
    record_ids = [item.get("record_id") for item in specs if isinstance(item, dict)]
    if len(record_ids) != 7 or len(set(record_ids)) != 7:
        raise ContractError("run template record IDs must be unique")

    implementation = payload.get("implementation_freeze")
    if not isinstance(implementation, dict):
        raise ContractError("implementation_freeze must be an object")
    for name in ("I_B_allowed_paths", "I_S_allowed_paths"):
        paths = implementation.get(name)
        if (
            not isinstance(paths, list)
            or not paths
            or len(paths) != len(set(paths))
            or not all(isinstance(path, str) for path in paths)
        ):
            raise ContractError(f"{name} must be a unique path array")
    if implementation.get("economic_run_before_I_B_I_S_R_frozen") != "forbidden":
        raise ContractError("pre-freeze economic execution is not forbidden")

    checkpoint = payload.get("checkpoint_and_lease_protocol")
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("schema_version") != 2
        or checkpoint.get("current_run_attempt_must_equal_one") is not True
        or checkpoint.get("result_prevents_takeover") is not True
    ):
        raise ContractError("checkpoint and lease protocol is not v2 fail-closed")


def load_preregistration(
    path: str | Path,
    *,
    repository: str | Path | None = None,
    expected_keys: Collection[str] | None = None,
) -> dict[str, Any]:
    payload = strict_json_load(path)
    if not isinstance(payload, dict):
        raise ContractError("preregistration root must be an object")
    if expected_keys is not None:
        require_exact_keys(payload, expected_keys, label="preregistration")
    else:
        validate_preregistration(payload, repository=repository)
    return payload


def _safe_repo_path(root: Path, raw_path: str) -> Path:
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ContractError(f"path must be a normalized repo-relative path: {raw_path}")
    if pure.as_posix() != raw_path or any(part in {"", "."} for part in pure.parts):
        raise ContractError(f"path must be a normalized repo-relative path: {raw_path}")
    return root.joinpath(*pure.parts)


def _is_sha(value: str, length: int | None = None) -> bool:
    return (length is None or len(value) == length) and bool(value) and all(
        character in "0123456789abcdef" for character in value
    )


def _git_output(root: Path, arguments: list[str]) -> bytes:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"")
        message = detail.decode("utf-8", "replace").strip()
        raise ContractError(f"Git identity check failed: {message or exc}") from exc


def _validate_git_file_identity(
    root: Path,
    revision: str,
    relative: str,
    identity: Mapping[str, Any],
    *,
    path_is_embedded: bool = False,
) -> None:
    _safe_repo_path(root, relative)
    record = _git_output(root, ["ls-tree", "-z", revision, "--", relative])
    entries = [entry for entry in record.split(b"\0") if entry]
    if len(entries) != 1:
        raise ContractError(f"frozen path is absent or ambiguous: {relative}")
    metadata, separator, raw_path = entries[0].partition(b"\t")
    fields = metadata.split(b" ")
    if not separator or len(fields) != 3 or raw_path != relative.encode("utf-8"):
        raise ContractError(f"malformed frozen Git identity: {relative}")
    mode, object_type, oid = (field.decode("ascii") for field in fields)
    if object_type != "blob" or mode != identity.get("mode"):
        raise ContractError(f"frozen mode/type mismatch: {relative}")
    if oid != identity.get("git_blob") or not _is_sha(oid):
        raise ContractError(f"frozen blob mismatch: {relative}")
    content = _git_output(root, ["cat-file", "blob", oid])
    if hashlib.sha256(content).hexdigest() != identity.get("sha256"):
        raise ContractError(f"frozen full-byte SHA-256 mismatch: {relative}")
    if path_is_embedded and identity.get("path") != relative:
        raise ContractError(f"embedded frozen path mismatch: {relative}")


def economic_tree_manifest(
    revision: str,
    allowlist: Collection[str],
    *,
    repository: str | Path = ".",
) -> list[dict[str, str]]:
    """Build the exact Git-tree-minus-allowlist manifest frozen in P."""
    root = Path(repository)
    if (
        not isinstance(revision, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", revision) is None
        or ".." in revision
        or "//" in revision
    ):
        raise ContractError("Git revision has an unsafe syntax")
    allowed: set[bytes] = set()
    for path in allowlist:
        _safe_repo_path(root, path)
        encoded = path.encode("utf-8")
        if encoded in allowed:
            raise ContractError(f"duplicate allowlist path: {path}")
        allowed.add(encoded)
    try:
        completed = subprocess.run(
            ["git", "ls-tree", "-rz", "--full-tree", revision],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"")
        message = detail.decode("utf-8", "replace").strip()
        raise ContractError(f"cannot read Git tree {revision}: {message or exc}") from exc

    records_with_paths: list[tuple[bytes, dict[str, str]]] = []
    seen: set[bytes] = set()
    for entry in completed.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3 or not path:
            raise ContractError("git ls-tree returned a malformed record")
        mode, object_type, object_oid = fields
        if path in seen:
            raise ContractError("git tree contains a duplicate path")
        seen.add(path)
        if path in allowed:
            continue
        try:
            mode_text = mode.decode("ascii")
            type_text = object_type.decode("ascii")
            oid_text = object_oid.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ContractError("git tree metadata must be ASCII") from exc
        if len(mode_text) != 6 or not mode_text.isdigit():
            raise ContractError(f"invalid Git mode: {mode_text}")
        if type_text not in {"blob", "commit", "tree"}:
            raise ContractError(f"invalid Git object type: {type_text}")
        if not oid_text or any(character not in "0123456789abcdef" for character in oid_text):
            raise ContractError("invalid Git object OID")
        records_with_paths.append(
            (
                path,
                {
                    "path_b64": base64.b64encode(path).decode("ascii"),
                    "mode": mode_text,
                    "object_type": type_text,
                    "object_oid": oid_text,
                },
            )
        )
    return [record for _, record in sorted(records_with_paths, key=lambda item: item[0])]


def economic_tree_hash(
    revision: str,
    allowlist: Collection[str],
    *,
    repository: str | Path = ".",
) -> str:
    return canonical_payload_hash(
        economic_tree_manifest(revision, allowlist, repository=repository)
    )


def render_bound_argv(
    template: Sequence[str],
    values: Mapping[str, str],
    *,
    allowed_slots: Collection[str],
) -> list[str]:
    """Expand only explicitly frozen late-bound argv placeholders."""
    allowed = set(allowed_slots)
    formatter = string.Formatter()
    required: set[str] = set()
    for argument in template:
        if not isinstance(argument, str) or "\0" in argument:
            raise ContractError("argv entries must be NUL-free strings")
        for _, field_name, format_spec, conversion in formatter.parse(argument):
            if field_name is None:
                continue
            if not field_name or format_spec or conversion or field_name not in allowed:
                raise ContractError(f"undeclared placeholder in argv: {field_name}")
            required.add(field_name)
    unknown_values = set(values) - allowed
    if unknown_values:
        raise ContractError(f"unknown late-bound slot values: {sorted(unknown_values)}")
    missing = required - set(values)
    if missing:
        raise ContractError(f"missing late-bound slot values: {sorted(missing)}")
    for key, value in values.items():
        if not isinstance(value, str) or "\0" in value:
            raise ContractError(f"late-bound slot {key} must be a NUL-free string")
    try:
        return [argument.format_map(dict(values)) for argument in template]
    except (KeyError, ValueError) as exc:
        raise ContractError(f"cannot render frozen argv: {exc}") from exc


def binding_identity(binding: Mapping[str, Any]) -> str:
    """Hash every R binding field except its non-self-referential signature."""
    return canonical_payload_hash(
        {key: value for key, value in binding.items() if key != "record_signature"}
    )


def validate_binding(
    binding: Mapping[str, Any],
    expected_sha256: str | None = None,
    *,
    expected_keys: Collection[str] = RUN_BINDING_KEYS,
    allowed_late_slots: Collection[str] = (),
) -> None:
    require_exact_keys(binding, expected_keys, label="run binding")
    for label in ("record_id", "candidate_id", "logical_run_id", "source_alias"):
        if not isinstance(binding[label], str) or not binding[label]:
            raise ContractError(f"run binding {label} must be nonempty")
    for label in ("source_revision", "source_tree"):
        value = binding[label]
        if not isinstance(value, str) or not _is_sha(value, 40):
            raise ContractError(f"run binding {label} must be a 40-character SHA")

    blobs = binding["source_blob_identities"]
    if not isinstance(blobs, dict):
        raise ContractError("source_blob_identities must be an object")
    for path, identity in blobs.items():
        if not isinstance(path, str) or not isinstance(identity, dict):
            raise ContractError("source blob identity is malformed")
        _safe_repo_path(Path("."), path)
        require_exact_keys(
            identity, SOURCE_BLOB_IDENTITY_KEYS, label=f"source blob {path}"
        )
        if (
            identity["mode"] != "100644"
            or not isinstance(identity["git_blob"], str)
            or not _is_sha(identity["git_blob"], 40)
            or not isinstance(identity["sha256"], str)
            or not _is_sha(identity["sha256"], 64)
        ):
            raise ContractError(f"source blob identity has invalid values: {path}")

    for label in (
        "P", "workflow", "resolved_inputs", "runtime", "attempt_policy", "paths",
        "canonical_payload_schema", "exit_semantics", "item_manifest_contract",
    ):
        if not isinstance(binding[label], dict):
            raise ContractError(f"run binding {label} must be an object")
        _validate_json_value(binding[label], f"$.{label}")
    for label in (
        "scenario_manifest_identity", "synthetic_control_manifest_identity",
        "evaluation_manifest_identity",
    ):
        identity = binding[label]
        if identity is None:
            continue
        if not isinstance(identity, dict):
            raise ContractError(f"{label} must be an object or null")
        require_exact_keys(identity, MANIFEST_IDENTITY_KEYS, label=label)
        if (
            type(identity["count"]) is not int
            or type(identity["unique_count"]) is not int
            or identity["count"] < 0
            or identity["unique_count"] != identity["count"]
            or not isinstance(identity["sha256"], str)
            or not _is_sha(identity["sha256"], 64)
        ):
            raise ContractError(f"{label} has invalid values")

    argv = binding["argv"]
    late = binding["runtime_late_slots"]
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise ContractError("run binding argv must be a string array")
    if not isinstance(late, list) or len(late) != len(set(late)) or not all(
        isinstance(item, str) for item in late
    ):
        raise ContractError("run binding late_bound_slots must be unique strings")
    if not set(late) <= set(allowed_late_slots):
        raise ContractError("run binding contains an undeclared late-bound slot")
    # Parsing with harmless sentinel values proves every placeholder is declared.
    render_bound_argv(argv, {slot: "x" for slot in late}, allowed_slots=late)

    signature = binding["record_signature"]
    if not isinstance(signature, str) or not _is_sha(signature, 64):
        raise ContractError("run binding signature must be a SHA-256")
    if expected_sha256 is not None and signature != expected_sha256:
        raise ContractError("run binding expected signature mismatch")
    if binding_identity(binding) != signature:
        raise ContractError("run binding identity mismatch")


def load_run_bindings(
    path: str | Path, *, allowed_late_slots: Collection[str] = RUNTIME_LATE_SLOTS
) -> dict[str, Any]:
    """Load the exact implementation-stage R schema and validate every record."""
    payload = strict_json_load(path)
    if not isinstance(payload, dict):
        raise ContractError("run bindings root must be an object")
    require_exact_keys(payload, RUN_BINDINGS_ROOT_KEYS, label="run bindings")
    if payload["schema_version"] != 2 or payload["kind"] != "c6_run_bindings":
        raise ContractError("unsupported run bindings schema")
    if payload["status"] != "frozen_before_economic_dispatch":
        raise ContractError("run bindings are not frozen")
    p_identity = payload["P"]
    if not isinstance(p_identity, dict):
        raise ContractError("run bindings P identity must be an object")
    require_exact_keys(p_identity, P_IDENTITY_KEYS, label="run bindings P identity")
    for label in ("commit", "tree"):
        value = p_identity[label]
        if not isinstance(value, str) or not _is_sha(value, 40):
            raise ContractError(f"run bindings P {label} is invalid")
    for label, length in (("blob", 40), ("sha256", 64)):
        value = p_identity[label]
        if not isinstance(value, str) or not _is_sha(value, length):
            raise ContractError(f"run bindings P {label} is invalid")

    for label in (
        "workflow", "implementations", "cross_record_invariants",
        "selection_validator",
    ):
        if not isinstance(payload[label], dict):
            raise ContractError(f"run bindings {label} must be an object")
    if set(payload["implementations"]) != {"I_B", "I_S"}:
        raise ContractError("run bindings implementations must be exact I_B/I_S")

    bindings = payload["binding_records"]
    if not isinstance(bindings, list) or len(bindings) != 7:
        raise ContractError("run bindings must contain exactly seven records")
    seen: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ContractError("each run binding must be an object")
        validate_binding(binding, allowed_late_slots=allowed_late_slots)
        binding_id = binding["record_id"]
        if binding_id in seen:
            raise ContractError(f"duplicate binding_id: {binding_id}")
        seen.add(binding_id)
    if tuple(item["record_id"] for item in bindings) != BINDING_RECORD_ORDER:
        raise ContractError("run binding record order does not match P")
    first = bindings[0]
    for binding in bindings:
        if binding["P"] != p_identity or binding["workflow"] != payload["workflow"]:
            raise ContractError("record P/workflow identity differs from R root")
        alias = binding["source_alias"]
        implementation = payload["implementations"].get(alias)
        if not isinstance(implementation, dict):
            raise ContractError("record source_alias has no implementation")
        if any(
            binding[key] != implementation[value]
            for key, value in (
                ("source_revision", "commit"),
                ("source_tree", "tree"),
                ("source_blob_identities", "required_blobs"),
            )
        ):
            raise ContractError("record source identity differs from implementation")
        expected_alias = "I_B" if binding["candidate_id"] == "C6-Base" else "I_S"
        if alias != expected_alias:
            raise ContractError("candidate/source_alias mapping is invalid")
        for key in ("resolved_inputs", "runtime"):
            if binding[key] != first[key]:
                raise ContractError(f"cross-record {key} differs")
    return payload


def select_binding(
    run_bindings: Mapping[str, Any], binding_id: str, *, candidate_id: str | None = None
) -> dict[str, Any]:
    matches = [
        item
        for item in run_bindings.get("binding_records", [])
        if isinstance(item, dict)
        and item.get("workflow_binding_id") == binding_id
        and (candidate_id is None or item.get("candidate_id") == candidate_id)
    ]
    if len(matches) != 1:
        raise ContractError(f"binding_id must resolve exactly once: {binding_id}")
    return matches[0]


def validate_selection_shape(selection: Mapping[str, Any], preregistration: Mapping[str, Any]) -> None:
    """Enforce all five frozen D branches without excusing correctness failures."""
    specs = preregistration["diagnostic_predicate_manifests"]["L1_APPLICABLE_DIAGNOSTIC_PREDICATES"]
    expected_ids = [spec["id"] for spec in specs]
    def failures(rows: Any) -> set[str]:
        if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or [row.get("predicate_id") for row in rows] != expected_ids
            or any(type(row.get("passed")) is not bool for row in rows)):
            raise ContractError("D predicate array is incomplete or out of manifest order")
        return {row["predicate_id"] for row in rows if not row["passed"]}
    base_failures = failures(selection["base_l1_predicates"])
    residual = bool(selection["residual_ids"])
    deferred = {spec["id"] for spec in specs if spec.get("selection_defer_if_base_residual_exists") is True} if residual else set()
    base_failed = bool(base_failures - deferred)
    qualification = selection["s_qualification"] is not None
    s_present = selection["base_plus_s_l1"] is not None
    s_rows = selection["base_plus_s_l1_predicates"]
    if s_present != (s_rows is not None):
        raise ContractError("D S artifact/predicate presence is inconsistent")
    s_failed = bool(failures(s_rows)) if s_present else False
    variants = {
        "BASE_REJECTED": (base_failed and not qualification and not s_present, None, "BASE_L1_PREDICATE_FAILED"),
        "BASE_SELECTED": (not residual and not base_failures and not qualification and not s_present, "C6-Base", None),
        "QUALIFICATION_REJECTED": (residual and not base_failed and qualification and not s_present, None, "S_QUALIFICATION_FAILED"),
        "BASE_PLUS_S_REJECTED": (residual and not base_failed and qualification and s_present and s_failed, None, "BASE_PLUS_S_L1_PREDICATE_FAILED"),
        "BASE_PLUS_S_SELECTED": (residual and not base_failed and qualification and s_present and not s_failed, "C6-Base+S", None),
    }
    branch = selection.get("branch")
    if not isinstance(branch, str) or branch not in variants:
        raise ContractError("D branch is missing or unknown")
    valid, candidate, reason = variants[branch]
    if (not valid or selection["status"] != ("selected" if candidate else "rejected")
        or selection["selected_candidate"] != candidate
        or (selection["C"] is not None) != (candidate is not None)
        or selection["rejection_reasons"] != ([] if reason is None else [reason])):
        raise ContractError("D branch is internally inconsistent")


def validate_selection_producer_payloads(
    selection: Mapping[str, Any], base: Mapping[str, Any], qualification: Mapping[str, Any] | None,
    selected_s: Mapping[str, Any] | None, scenario_ids: Sequence[str],
) -> None:
    """Compare D claims with already authenticated, sealed producer contents."""
    selected_base = [row for row in base["evaluations"] if row["variant_id"] == "C6-Base"]
    residuals = sorted(row["scenario_id"] for row in selected_base
                       if abs(float(row["official_metrics"]["max_drawdown"])) > 0.18 + 1e-15)
    if ([row["scenario_id"] for row in selected_base] != list(scenario_ids)
            or len(set(scenario_ids)) != len(scenario_ids)
            or selection["residual_ids"] != residuals
            or selection["base_l1_predicates"] != base["diagnostic_predicates"]):
        raise ContractError("D differs from sealed Base predicates or residual identity")
    if ((qualification is not None) != (selection["s_qualification"] is not None)
            or (selected_s is not None) != (selection["base_plus_s_l1"] is not None)):
        raise ContractError("D has missing or extra sealed producers")
    def producer(identity: Mapping[str, Any]) -> dict[str, Any]:
        return {key: identity[key] for key in ("artifact_full_byte_sha256", "attempt_id", "logical_run_id", "workflow_run_id")} | {"binding_id": identity["record_id"]}
    if qualification is not None:
        rows = qualification["results"]
        recomputed = [len(row["criteria"]) == 7 and all(item["passed"] is True for item in row["criteria"]) for row in rows]
        if (qualification["base_producer_identity"] != producer(selection["base_l1"])
                or qualification["residual_ids"] != residuals
                or [row["scenario_id"] for row in rows] != residuals
                or any(row["passed"] is not passed for row, passed in zip(rows, recomputed))
                or qualification["all_passed"] is not all(recomputed)
                or qualification["all_passed"] is not (selection["branch"] != "QUALIFICATION_REJECTED")):
            raise ContractError("D differs from sealed qualification evidence")
    if selected_s is not None:
        if (selected_s["diagnostic_predicates"] != selection["base_plus_s_l1_predicates"]
                or selected_s["base_producer_identity"] != producer(selection["base_l1"])
                or selected_s["qualification_producer_identity"] != producer(selection["s_qualification"])):
            raise ContractError("D differs from sealed S predicates or producer chain")


def validate_selection_commit(
    path: str | Path,
    *,
    run_bindings: Mapping[str, Any],
    run_bindings_revision: str,
    candidate_id: str,
) -> dict[str, Any]:
    """Validate D's tagged union and return the exact selected implementation."""
    selection = strict_json_load(path)
    if not isinstance(selection, dict):
        raise ContractError("D selection must be an object")
    keys = {
        "schema_version", "kind", "branch", "status", "P", "R", "base_l1",
        "residual_ids", "residual_ids_sha256", "base_l1_predicates",
        "s_qualification", "base_plus_s_l1", "base_plus_s_l1_predicates",
        "selected_candidate", "C", "rejection_reasons",
    }
    require_exact_keys(selection, keys, label="D selection")
    if selection["schema_version"] != 2 or selection["kind"] != "c6_selection":
        raise ContractError("D selection has the wrong schema")
    if selection["P"] != run_bindings["P"]:
        raise ContractError("D selection P identity differs from R")
    r_identity = selection["R"]
    if not isinstance(r_identity, dict) or r_identity.get("commit") != run_bindings_revision:
        raise ContractError("D selection R identity differs from runtime R")
    residuals = selection["residual_ids"]
    if not isinstance(residuals, list) or residuals != sorted(set(residuals)):
        raise ContractError("D residual IDs are not unique and lexicographic")
    residual_hash = hashlib.sha256(
        "".join(f"{item}\n" for item in residuals).encode()
    ).hexdigest()
    if selection["residual_ids_sha256"] != residual_hash:
        raise ContractError("D residual identity is invalid")
    if selection["status"] != "selected" or selection["selected_candidate"] != candidate_id:
        raise ContractError("D did not select this candidate")
    alias = "I_B" if candidate_id == "C6-Base" else "I_S"
    implementation = run_bindings["implementations"][alias]
    if selection["C"] != implementation:
        raise ContractError("D selected implementation differs from R")
    preregistration = load_preregistration("artifacts/diagnostics/c6-preregistration.json")
    validate_selection_shape(selection, preregistration)
    return implementation
