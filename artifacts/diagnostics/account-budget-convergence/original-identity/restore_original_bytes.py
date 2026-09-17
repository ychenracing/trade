"""Restore exact retained originals from immutable transport bytes, without replay.

The binary residual is derived from the local originals. It restores historical
bytes; it never edits strategy results, tolerances, or any production artifact.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import lzma
from pathlib import Path
import shutil
import struct
import tarfile


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check(data: bytes, size: int, sha256: str) -> None:
    if len(data) != size or digest(data) != sha256:
        raise ValueError('Original or transport byte identity mismatch')


def join_parts(root: Path, filename: str) -> tuple[bytes, dict]:
    manifest = json.loads((root / filename).read_text())
    out = bytearray()
    for part in manifest['parts']:
        if part['offset'] != len(out):
            raise ValueError('Non-contiguous transport part')
        path = Path(part['path'])
        if path.name != str(path):
            raise ValueError('Unsafe transport path')
        data = (root / path).read_bytes()
        check(data, part['size'], part['sha256'])
        oid = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
        if oid != part['git_blob_sha1']:
            raise ValueError('Git object identity mismatch')
        out.extend(data)
    check(out, manifest['size'], manifest['sha256'])
    return bytes(out), manifest


def apply_delta(base: bytes, delta: bytes, expected_size: int) -> bytes:
    out = bytearray()
    cursor = 0
    while cursor < len(delta):
        command = delta[cursor:cursor + 1]
        cursor += 1
        if command == b'I':
            count, = struct.unpack_from('<I', delta, cursor)
            cursor += 4
            block = delta[cursor:cursor + count]
            cursor += count
        elif command in (b'C', b'D'):
            offset, count = struct.unpack_from('<II', delta, cursor)
            cursor += 8
            block = base[offset:offset + count]
            if command == b'D':
                residual = delta[cursor:cursor + count]
                cursor += count
                if len(residual) != count:
                    raise ValueError('Truncated residual')
                block = bytes((x + y) & 255 for x, y in zip(block, residual))
        else:
            raise ValueError('Unknown transport opcode')
        if len(block) != count or len(out) + count > expected_size:
            raise ValueError('Out-of-bounds transport operation')
        out.extend(block)
    if len(out) != expected_size:
        raise ValueError('Truncated original')
    return bytes(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--base-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    packed, transport = join_parts(args.metadata, 'byte-delta-manifest.json')
    content = lzma.decompress(packed)
    header_size, = struct.unpack_from('<I', content)
    manifest = json.loads(content[4:4 + header_size])
    payload = content[4 + header_size:]
    check(payload, manifest['delta_bytes'], manifest['delta_sha256'])
    base_archive = args.base_dir / 'rejected-originals.tar.xz'
    base_bytes = base_archive.read_bytes()
    if digest(base_bytes) != manifest['base_archive_sha256']:
        raise ValueError('Wrong immutable base artifact')
    source_bytes, source_transport = join_parts(args.metadata, 'identity-transport-manifest.json')
    with tarfile.open(fileobj=io.BytesIO(source_bytes), mode='r:xz') as archive:
        stream = archive.extractfile('manifest.json')
        if stream is None:
            raise ValueError('Missing original identities')
        source_manifest = json.load(stream)
    originals = {row['path']: row for row in source_manifest['raws']}
    if set(originals) != {row['path'] for row in manifest['records']}:
        raise ValueError('Incomplete original coverage')
    args.out.mkdir(parents=True, exist_ok=False)
    receipts = []
    with tarfile.open(fileobj=io.BytesIO(base_bytes), mode='r:xz') as archive:
        for row in manifest['records']:
            path = Path(row['path'])
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe original path')
            stream = archive.extractfile(str(path))
            if stream is None:
                raise ValueError('Missing base bytes')
            base = stream.read()
            check(base, row['base_size'], row['base_sha256'])
            if row['gzip']:
                base = gzip.decompress(base)
            check(base, row['decoded_base_size'], row['decoded_base_sha256'])
            offset, size = row['delta_offset'], row['delta_size']
            delta = payload[offset:offset + size]
            check(delta, size, row['delta_sha256'])
            target = apply_delta(base, delta, row['decoded_target_size'])
            check(target, row['decoded_target_size'], row['decoded_target_sha256'])
            if row['gzip']:
                target = gzip.compress(target, compresslevel=9, mtime=0)
            identity = originals[str(path)]
            check(target, identity['size'], identity['sha256'])
            output = args.out / path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(target)
            receipts.append({'path': str(path), 'size': len(target), 'sha256': digest(target),
                             'git_blob_sha1': hashlib.sha1(f'blob {len(target)}\0'.encode() + target).hexdigest()})
    shutil.copytree(args.metadata, args.out / 'transport')
    (args.out / 'source-and-raw-identities.tar.xz').write_bytes(source_bytes)
    receipt = {'kind': 'original_byte_transport_readback', 'canonical': False,
               'economic_acceptance': False, 'originals_exact': True,
               'base_artifact_id': manifest['base_artifact_id'],
               'base_archive_sha256': manifest['base_archive_sha256'],
               'delta_archive_sha256': transport['sha256'],
               'source_archive_sha256': source_transport['sha256'],
               'source_identities': source_manifest['identities'], 'originals': receipts}
    (args.out / 'preservation-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(f'Preserved {len(receipts)} complete original files; all exact hashes verified.')


if __name__ == '__main__':
    main()
