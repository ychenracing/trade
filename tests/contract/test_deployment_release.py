"""Release evidence must be recoverable from the committed bytes."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from quantfusion.application.c6_contract import canonical_payload_hash

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = (
    ROOT / "artifacts/diagnostics/no_waiver/production-primary/acceptance-verification"
)


@pytest.mark.parametrize("directory_name,manifest_name", [
    ("deployment-originals", "deployment-external-manifest.json"),
    ("deployment-formal-originals", "deployment-formal-manifest.json"),
])
def test_original_archive_reconstructs_and_every_member_matches(directory_name, manifest_name):
    directory = EVIDENCE / directory_name
    manifest = json.loads((directory / "manifest.json").read_text())
    archive = bytearray()
    for part in manifest["parts"]:
        raw = (directory / part["name"]).read_bytes()
        assert part["offset"] == len(archive)
        assert len(raw) == part["bytes"]
        assert hashlib.sha256(raw).hexdigest() == part["sha256"]
        assert (
            hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            == part["git_blob"]
        )
        archive.extend(raw)
    assert len(archive) == manifest["bytes"]
    assert hashlib.sha256(archive).hexdigest() == manifest["sha256"]
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as stored:
        contents = json.load(stored.extractfile(manifest_name))
        assert len(stored.getmembers()) == len(contents["members"]) + 1
        for member in contents["members"]:
            raw = stored.extractfile(member["path"]).read()
            assert len(raw) == member["bytes"]
            assert hashlib.sha256(raw).hexdigest() == member["sha256"]
        if directory_name == "deployment-formal-originals":
            receipt = json.loads((ROOT / "artifacts/validation/deployment-release-receipt.json").read_text())
            raw = stored.extractfile("universe_stress.json").read()
            assert hashlib.sha256(raw).hexdigest() == receipt["original_file_sha256"]
            assert canonical_payload_hash(json.loads(raw)) == receipt["payload_sha256"]
