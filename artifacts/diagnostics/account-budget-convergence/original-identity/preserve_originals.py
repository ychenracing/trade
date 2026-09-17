"""Reconstruct retained source/results; accept only exact original bytes.

This is an evidence recovery tool, not an economic candidate or a release gate.
It never promotes a policy or modifies production source/thresholds.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.check_output(['git', '-C', str(root), *args], input=data)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def restore_sources(repo: Path, meta: Path, work: Path, manifest: dict) -> dict[str, Path]:
    sources = {}
    # The protective snapshot's local parent is also retained, not fabricated.
    body = (meta / 'identities/baseline-commit.txt').read_bytes()
    assert git(repo, 'hash-object', '-w', '-t', 'commit', '--stdin', data=body).decode().strip() == manifest['identities']['baseline']['revision']
    for name, identity in manifest['identities'].items():
        root = work / name
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout', str(repo), str(root)], check=True)
        git(root, 'checkout', '--detach', manifest['base_remote'])
        patch = meta / f'sources/{name}.patch'
        assert sha(patch.read_bytes()) == identity['patch_sha256']
        if patch.stat().st_size:
            git(root, 'apply', '--whitespace=error', str(patch))
        git(root, 'add', '-A')
        assert git(root, 'write-tree').decode().strip() == identity['tree'], name
        body = (meta / f'identities/{name}-commit.txt').read_bytes()
        assert git(root, 'hash-object', '-w', '-t', 'commit', '--stdin', data=body).decode().strip() == identity['revision'], name
        git(root, 'checkout', '--detach', identity['revision'])
        sources[name] = root
    return sources


def reproduce(item: dict, meta: Path, work: Path, sources: dict[str, Path]) -> dict:
    path = Path(item['path'])
    output = work / path
    if path.parent.name == 'budget-evidence':
        name = path.name.removesuffix('.pickle')
        mode, scenario = name.split('-', 1)
        source = sources['observe' if mode == 'absent' else mode]
        log = work / f'{name}.log'
        with log.open('w') as stream:
            subprocess.run([sys.executable, str(meta / 'measurement/run_budget_pair.py'), str(source), mode, scenario, str(output.parent)], stdout=stream, stderr=subprocess.STDOUT, check=True)
    else:
        # Load the exact original formal capture function in an isolated process.
        # Only container-specific paths are relocated; the source SHA and all
        # scenario/config/cost arguments remain fixed by the retained program.
        script = (meta / 'measurement/run_budget_scope.py').read_text()
        script = script.replace('/mnt/data/trade-observe-screen', str(sources['observe']))
        script = script.replace('/mnt/data/budget-formal-observe', str(work / 'budget-formal-observe'))
        script = script.replace('/mnt/data/budget-evidence', str(work / 'budget-evidence'))
        sid = path.name.removesuffix('.pickle.gz')
        call = '\nscenes=stress_scenarios._multi_seed_scenarios(random_samples=50,permutation_samples=50,seeds=(20260807,20260817,20260827))\nrun(next(s for s in scenes if s["scenario_id"] == ' + repr(sid) + '))\n'
        program = work / f'capture-{sid}.py'
        program.write_text(script.replace("if __name__=='__main__':", "if False:") + call)
        output.parent.mkdir(exist_ok=True)
        with (work / f'capture-{sid}.log').open('w') as stream:
            subprocess.run([sys.executable, str(program)], stdout=stream, stderr=subprocess.STDOUT, check=True)
    data = output.read_bytes()
    return {**item, 'observed_size': len(data), 'observed_sha256': sha(data),
            'byte_identity_verified': len(data) == item['size'] and sha(data) == item['sha256']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    repo, meta_dir, work = args.repo.resolve(), args.metadata.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    transport = json.loads((meta_dir / 'identity-transport-manifest.json').read_text())
    payload = bytearray()
    for item in transport['parts']:
        block = (meta_dir / item['path']).read_bytes()
        assert item['offset'] == len(payload) and len(block) == item['size']
        assert sha(block) == item['sha256']
        assert hashlib.sha1(f'blob {len(block)}\0'.encode() + block).hexdigest() == item['git_blob_sha1']
        payload.extend(block)
    assert len(payload) == transport['size'] and sha(payload) == transport['sha256']
    meta = work / 'metadata'
    with tarfile.open(fileobj=io.BytesIO(lzma.decompress(payload))) as archive:
        archive.extractall(meta, filter='data')
    manifest = json.loads((meta / 'manifest.json').read_text())
    if '.'.join(map(str, sys.version_info[:3])) != manifest['runtime']['python']:
        raise ValueError('The original pickle runtime must match exactly')
    import numpy
    import pandas
    if numpy.__version__ != manifest['runtime']['numpy'] or pandas.__version__ != manifest['runtime']['pandas']:
        raise ValueError('The original numerical library versions must match exactly')
    sources = restore_sources(repo, meta, work, manifest)
    records = []
    for parent in ('budget-evidence', 'budget-formal-observe'):
        with ThreadPoolExecutor(max_workers=2) as pool:
            for row in pool.map(lambda x: reproduce(x, meta, work, sources),
                                [r for r in manifest['raws'] if Path(r['path']).parent.name == parent]):
                records.append(row)
                print(row['path'], row['byte_identity_verified'], flush=True)
    verified = all(r['byte_identity_verified'] for r in records)
    receipt = {'kind': 'original_byte_identity_preservation', 'verified': verified,
               'economic_acceptance': False, 'canonical': False,
               'source_archive_sha256': transport['sha256'], 'identities': manifest['identities'],
               'github_measurement_transport_commit': os.environ.get('GITHUB_SHA'),
               'runtime': manifest['runtime'], 'results': records}
    (work / 'verification.json').write_text(json.dumps(receipt, indent=2) + '\n')
    if not verified:
        return 2
    # Stage only hash-matching originals. The consumer still verifies the Git
    # branch/ref separately; producing this directory is not a saved commit.
    staged = work / 'verified-originals'
    for row in records:
        output = staged / row['path']
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work / row['path'], output)
    shutil.copyfile(work / 'verification.json', staged / 'verification.json')
    shutil.copyfile(meta / 'manifest.json', staged / 'source-manifest.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
