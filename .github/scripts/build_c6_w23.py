from __future__ import annotations

from pathlib import Path
import sys

import yaml

W21 = "codex/c6-v21-workflow-anchor"
W23 = "codex/c6-v23-workflow-anchor"
BASE22_RUN = 34128460919
BASE22_SOURCE = "addb5c8ebf98ac43e08676cbc6ffe81a2627d9d7"
BASE22_WORKFLOW = "f9da08afebf22b3dc03a1fb3a0ec351a79adf42c"


def main(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(W21) != 2:
        raise SystemExit(f"unexpected W21 self-guard count: {text.count(W21)}")
    text = text.replace(W21, W23)

    marker = "  parallel_l1:\n"
    if text.count(marker) != 1:
        raise SystemExit("parallel_l1 marker drifted")
    guard = f'''  base_recovery_guard:\n    name: Authenticate Base22 recovery source\n    if: inputs.binding_id != 'c6.synthetic.resume'\n    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    steps:\n      - name: Verify fixed Base22 recovery set or no-op\n        if: inputs.binding_id == 'c6.base.l1'\n        env:\n          GITHUB_TOKEN: ${{{{ github.token }}}}\n        run: |\n          python3 - <<'PY'\n          import json\n          import os\n          import re\n          import urllib.request\n\n          repository = os.environ['GITHUB_REPOSITORY']\n          token = os.environ['GITHUB_TOKEN']\n          root = f'https://api.github.com/repos/{{repository}}/'\n          headers = {{\n              'Authorization': 'Bearer ' + token,\n              'Accept': 'application/vnd.github+json',\n              'X-GitHub-Api-Version': '2022-11-28',\n          }}\n\n          def get(path):\n              request = urllib.request.Request(root + path, headers=headers)\n              with urllib.request.urlopen(request, timeout=60) as response:\n                  return json.load(response)\n\n          source_run = {BASE22_RUN}\n          run = get(f'actions/runs/{{source_run}}')\n          expected_run = {{\n              'workflow_id': 349948458,\n              'head_branch': '{W21}',\n              'head_sha': '{BASE22_WORKFLOW}',\n              'event': 'workflow_dispatch',\n              'status': 'completed',\n              'conclusion': 'failure',\n              'run_attempt': 1,\n              'display_title': 'c6-bound-c6.base.l1-c6-v22-base-l1-a0',\n          }}\n          if any(run.get(key) != value for key, value in expected_run.items()):\n              raise SystemExit('Base22 recovery run identity drifted')\n\n          jobs = get(f'actions/runs/{{source_run}}/jobs?per_page=100')['jobs']\n          observed_jobs = {{}}\n          for job in jobs:\n              match = re.fullmatch(r'Parallel L1 core shard ([0-9]+)', str(job.get('name', '')))\n              if match:\n                  index = int(match.group(1))\n                  observed_jobs[index] = (job.get('status'), job.get('conclusion'))\n          if observed_jobs != {{index: ('completed', 'success') for index in range(12)}}:\n              raise SystemExit('Base22 shard job set is not exactly 12 successful jobs')\n\n          expected = {{\n              0: (10024330850, 'sha256:6418073aefd56eeb6dc92a7e1a3adafbf84c4b1fd8559628d878d244e9dbe8db'),\n              1: (10023438649, 'sha256:6cc87e127e50e80d136d7e82e9a25d4ebca4640338a34f96301cfe8235a08d31'),\n              2: (10023713395, 'sha256:33a58d98e02f2fb7f9da68b483b681a19edf4d18fb382f0c54c9afe3511ece77'),\n              3: (10023684086, 'sha256:cbf20254308aa273f904ead8e5c2168355a3e43a31147e87efbf69aa1e536f5d'),\n              4: (10023697635, 'sha256:bdc2a88db800adcfade4b07bbeece8ed1864e4816855c2b214f546828bb59710'),\n              5: (10024300356, 'sha256:232c334a3607392073bb5945a223287791fa9ef227fe0ec0f7b3ed9c159ca77a'),\n              6: (10023650878, 'sha256:d7c4928c767d34cba6e0c7e8ac3316a42a9d12dba09645904d148e2c66377324'),\n              7: (10022784784, 'sha256:f8cff24b50c13341b64bce5397df1f09825a54b6344e47f3e398763f2629f572'),\n              8: (10023716462, 'sha256:1a2f640104a81044ace3f9dcc21991d4c61c68dfa6ff6d9a4861db38832b7cc4'),\n              9: (10023686351, 'sha256:1e4ea7babc79c833b200152f1b9ad0bd0ddbde813163fb1909a31fa10d45837a'),\n              10: (10024256138, 'sha256:eadb1218d868f7ccda9e2f1dbf2bec85f8c68ff2f052b00dfca49e6bac9caf3a'),\n              11: (10024209166, 'sha256:d80514f7782a021f6c4801807ed6d9b1278054ba30c33fb9c0d61fce02231598'),\n          }}\n          artifacts = get(f'actions/runs/{{source_run}}/artifacts?per_page=100')['artifacts']\n          observed = {{}}\n          for artifact in artifacts:\n              match = re.fullmatch(r'c6-l1-shard-{BASE22_RUN}-([0-9]+)', str(artifact.get('name', '')))\n              if match:\n                  index = int(match.group(1))\n                  observed[index] = (artifact.get('id'), artifact.get('digest'))\n                  if artifact.get('expired') is not False:\n                      raise SystemExit('Base22 shard artifact expired')\n                  workflow_run = artifact.get('workflow_run') or {{}}\n                  if workflow_run.get('id') != source_run or workflow_run.get('head_sha') != expected_run['head_sha']:\n                      raise SystemExit('Base22 shard artifact workflow identity drifted')\n          if observed != expected or len(artifacts) != 12:\n              raise SystemExit('Base22 artifact ID/digest set drifted')\n          PY\n\n'''
    text = text.replace(marker, guard + marker)

    old_header = '''  parallel_l1:\n    name: Parallel L1 core shard ${{ matrix.shard }}\n    if: (inputs.binding_id == 'c6.base.l1' || inputs.binding_id == 'c6.base_plus_s.l1') && inputs.attempt_id == 'a0' && inputs.resume_from == '' && inputs.resume_workflow_run_id == ''\n    runs-on: ubuntu-24.04\n'''
    new_header = '''  parallel_l1:\n    name: Parallel L1 core shard ${{ matrix.shard }}\n    needs: [base_recovery_guard]\n    if: needs.base_recovery_guard.result == 'success' && (inputs.binding_id == 'c6.base.l1' || inputs.binding_id == 'c6.base_plus_s.l1') && inputs.attempt_id == 'a0' && inputs.resume_from == '' && inputs.resume_workflow_run_id == ''\n    runs-on: ubuntu-24.04\n'''
    if text.count(old_header) != 1:
        raise SystemExit("parallel_l1 header drifted")
    text = text.replace(old_header, new_header)

    old_compute = '''      - name: Compute exact chunk-preserving L1 shard\n        working-directory: source\n        env:\n          C6_BINDING_ID: ${{ inputs.binding_id }}\n          C6_SOURCE_REVISION: ${{ inputs.source_revision }}\n          C6_SHARD_INDEX: ${{ matrix.shard }}\n        run: |\n          set -euo pipefail\n          readarray -t runtime_values < <(python - "$GITHUB_WORKSPACE/bindings/artifacts/diagnostics/c6-run-bindings.json" "$C6_BINDING_ID" <<'PY'\n          import json, sys\n          payload=json.load(open(sys.argv[1], encoding='utf-8'))\n          rows=[r for r in payload['binding_records'] if r['workflow_binding_id']==sys.argv[2] and r['record_id']==sys.argv[2]]\n          if len(rows)!=1:\n              raise SystemExit('parallel L1 binding is ambiguous')\n          print(rows[0]['runtime']['workers'])\n          print(rows[0]['runtime']['checkpoint_every'])\n          PY\n          )\n          test "${runtime_values[0]}" = "4"\n          test "${runtime_values[1]}" = "10"\n          python -m quantfusion.application.c6_parallel_l1_cli \\\n            --preregistration artifacts/diagnostics/c6-preregistration.json \\\n            --bindings-file "$GITHUB_WORKSPACE/bindings/artifacts/diagnostics/c6-run-bindings.json" \\\n            --binding-record-id "$C6_BINDING_ID" \\\n            --source-revision "$C6_SOURCE_REVISION" \\\n            --shard-index "$C6_SHARD_INDEX" \\\n            --shard-count 12 \\\n            --workers "${runtime_values[0]}" \\\n            --output "$RUNNER_TEMP/c6-l1-shard/shard.json.gz"\n'''
    new_compute = f'''      - name: Recover exact Base22 shard bytes\n        if: inputs.binding_id == 'c6.base.l1'\n        uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093\n        with:\n          name: c6-l1-shard-{BASE22_RUN}-${{{{ matrix.shard }}}}\n          path: ${{{{ runner.temp }}}}/c6-l1-shard\n          github-token: ${{{{ github.token }}}}\n          run-id: {BASE22_RUN}\n          repository: ychenracing/trade\n      - name: Compute exact chunk-preserving L1 shard\n        if: inputs.binding_id == 'c6.base_plus_s.l1'\n        working-directory: source\n        env:\n          C6_BINDING_ID: ${{{{ inputs.binding_id }}}}\n          C6_SOURCE_REVISION: ${{{{ inputs.source_revision }}}}\n          C6_SHARD_INDEX: ${{{{ matrix.shard }}}}\n        run: |\n          set -euo pipefail\n          readarray -t runtime_values < <(python - "$GITHUB_WORKSPACE/bindings/artifacts/diagnostics/c6-run-bindings.json" "$C6_BINDING_ID" <<'PY'\n          import json, sys\n          payload=json.load(open(sys.argv[1], encoding='utf-8'))\n          rows=[r for r in payload['binding_records'] if r['workflow_binding_id']==sys.argv[2] and r['record_id']==sys.argv[2]]\n          if len(rows)!=1:\n              raise SystemExit('parallel L1 binding is ambiguous')\n          print(rows[0]['runtime']['workers'])\n          print(rows[0]['runtime']['checkpoint_every'])\n          PY\n          )\n          test "${{runtime_values[0]}}" = "4"\n          test "${{runtime_values[1]}}" = "10"\n          python -m quantfusion.application.c6_parallel_l1_cli \\\n            --preregistration artifacts/diagnostics/c6-preregistration.json \\\n            --bindings-file "$GITHUB_WORKSPACE/bindings/artifacts/diagnostics/c6-run-bindings.json" \\\n            --binding-record-id "$C6_BINDING_ID" \\\n            --source-revision "$C6_SOURCE_REVISION" \\\n            --shard-index "$C6_SHARD_INDEX" \\\n            --shard-count 12 \\\n            --workers "${{runtime_values[0]}}" \\\n            --output "$RUNNER_TEMP/c6-l1-shard/shard.json.gz"\n      - name: Semantically attest exact L1 shard\n        working-directory: source\n        env:\n          C6_BINDING_ID: ${{{{ inputs.binding_id }}}}\n          C6_SOURCE_REVISION: ${{{{ inputs.source_revision }}}}\n          C6_SHARD_INDEX: ${{{{ matrix.shard }}}}\n        run: |\n          set -euo pipefail\n          test -f "$RUNNER_TEMP/c6-l1-shard/shard.json.gz"\n          if [[ "$C6_BINDING_ID" = 'c6.base.l1' ]]; then\n            shard_source='{BASE22_SOURCE}'\n          else\n            shard_source="$C6_SOURCE_REVISION"\n          fi\n          python -m quantfusion.application.c6_parallel_l1_validate_cli \\\n            --preregistration artifacts/diagnostics/c6-preregistration.json \\\n            --bindings-file "$GITHUB_WORKSPACE/bindings/artifacts/diagnostics/c6-run-bindings.json" \\\n            --binding-record-id "$C6_BINDING_ID" \\\n            --validator-source-revision "$C6_SOURCE_REVISION" \\\n            --shard-source-revision "$shard_source" \\\n            --shard-index "$C6_SHARD_INDEX" \\\n            --shard-count 12 \\\n            --input "$RUNNER_TEMP/c6-l1-shard/shard.json.gz" \\\n            --output "$RUNNER_TEMP/c6-l1-shard/validation.json"\n'''
    if text.count(old_compute) != 1:
        raise SystemExit("parallel compute block drifted")
    text = text.replace(old_compute, new_compute)

    old_upload = "          path: ${{ runner.temp }}/c6-l1-shard/shard.json.gz\n"
    new_upload = "          path: ${{ runner.temp }}/c6-l1-shard/\n"
    if text.count(old_upload) != 1:
        raise SystemExit("parallel upload path drifted")
    text = text.replace(old_upload, new_upload)

    old_needs = "    needs: [parallel_l1]\n    if: inputs.binding_id != 'c6.synthetic.resume' && (needs.parallel_l1.result == 'success' || needs.parallel_l1.result == 'skipped')\n"
    new_needs = "    needs: [base_recovery_guard, parallel_l1]\n    if: inputs.binding_id != 'c6.synthetic.resume' && needs.base_recovery_guard.result == 'success' && (needs.parallel_l1.result == 'success' || needs.parallel_l1.result == 'skipped')\n"
    if text.count(old_needs) != 1:
        raise SystemExit("central needs block drifted")
    text = text.replace(old_needs, new_needs)

    yaml.safe_load(text)
    if text.count(W23) != 2 or text.count(W21) != 1:
        raise SystemExit("workflow/lineage anchor counts differ")
    for token in (
        "Authenticate Base22 recovery source",
        str(BASE22_RUN),
        BASE22_SOURCE,
        "c6_parallel_l1_validate_cli",
        "${{ github.token }}",
        "${{ matrix.shard }}",
    ):
        if token not in text:
            raise SystemExit(f"missing W23 token: {token}")
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_c6_w23.py WORKFLOW_PATH")
    main(Path(sys.argv[1]))
