"""Recover a hash-pinned rejected candidate without promoting production files."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def restore(packet_path, work, tests_path):
    record = json.loads(packet_path.read_text())
    observed = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=work, text=True).strip()
    if observed != record['revision']:
        raise ValueError('Unexpected research revision')
    allowed = {'quantfusion/risk/account_budget.py', 'quantfusion/strategy/trend.py',
               'tests/c6_non_economic/test_account_risk_budget.py',
               'tests/unit/test_observed_shock_budget.py'}
    if set(record['files']) != allowed:
        raise ValueError('Unexpected research edit paths')
    prepared = []
    for name, item in record['files'].items():
        path = work / name
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != item['before']:
            raise ValueError(f'Source drift: {name}')
        lines = source.decode().splitlines(keepends=True)
        previous = 0
        for begin, end, replacement in item['edits']:
            if not (previous <= begin <= end <= len(lines)):
                raise ValueError('Invalid or overlapping source edit')
            previous = end
        for begin, end, replacement in reversed(item['edits']):
            lines[begin:end] = replacement.splitlines(keepends=True)
        result = ''.join(lines).encode()
        if hashlib.sha256(result).hexdigest() != item['after']:
            raise ValueError(f'Restored bytes mismatch: {name}')
        prepared.append((path, result))
    if record['untracked_test'] != 'tests/unit/test_evidence_qualified_growth.py':
        raise ValueError('Unexpected test destination')
    destination = work / record['untracked_test']
    if destination.exists():
        raise ValueError('Refuse overwriting an unknown test')
    for path, data in prepared:
        path.write_bytes(data)
    shutil.copyfile(tests_path, destination)
    code = 'from quantfusion.application.stress_artifacts import _tree_fingerprint,_source_files; print(_tree_fingerprint(_source_files()))'
    fingerprint = subprocess.check_output([sys.executable, '-c', code], cwd=work, text=True).strip()
    if fingerprint != record['source_fingerprint']:
        raise ValueError('Restored candidate fingerprint mismatch')
    print(fingerprint)


if __name__ == '__main__':
    restore(*(Path(x) for x in sys.argv[1:]))
