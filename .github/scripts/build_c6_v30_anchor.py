from pathlib import Path
import subprocess, hashlib, json, yaml, ast, re, os
ROOT=Path.cwd()
OUT=Path(os.environ['C6_BUILD_OUTPUT']); OUT.mkdir(parents=True, exist_ok=True)
W29='01fb58613b44aee921c43291fe56203e76b52b0c'
IMAGE='python@sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970'
OS='oci-debian12-amd64'; VERSION='sha256-581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970'
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT).decode()
def replace_once(text,old,new):
 assert text.count(old)==1,(old,text.count(old))
 return text.replace(old,new)
bound=git('show',W29+':.github/workflows/c6-bound-economic.yml')
original=bound
start=bound.index('  base_recovery_guard:')
end=bound.index('  parallel_l1:',start)
fresh_guard='''  base_recovery_guard:
    name: Authenticate fresh-runtime execution boundary
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Require the v30 immutable boundary without importing old economics
        env:
          C6_WORKFLOW_REVISION: ${{ inputs.workflow_revision }}
          C6_LOGICAL_RUN_ID: ${{ inputs.logical_run_id }}
        run: |
          set -euo pipefail
          test "$GITHUB_REPOSITORY" = ychenracing/trade
          test "$GITHUB_EVENT_NAME" = workflow_dispatch
          test "$GITHUB_RUN_ATTEMPT" = 1
          test "$GITHUB_REF_NAME" = codex/c6-v30-workflow-anchor
          test "$GITHUB_SHA" = "$C6_WORKFLOW_REVISION"
          [[ "$C6_LOGICAL_RUN_ID" =~ ^c6-v30-[a-z0-9][a-z0-9.-]*$ ]]

'''
bound=bound[:start]+fresh_guard+bound[end:]
guard=f'''import hashlib, json, os, platform, sys, sysconfig
from pathlib import Path
expected_os = {OS!r}
expected_version = {VERSION!r}
if os.environ['C6_RUNNER_IMAGE_OS'] != expected_os or os.environ['C6_RUNNER_IMAGE_VERSION'] != expected_version:
    raise SystemExit('immutable OCI runtime identity mismatch')
if os.environ['C6_PYTHON_VERSION'] != '3.12.14' or platform.python_version() != '3.12.14':
    raise SystemExit('frozen Python patch mismatch')
if sys.implementation.name != 'cpython' or platform.system() != 'Linux' or platform.machine() != 'x86_64':
    raise SystemExit('OCI platform mismatch')
os_release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
if os_release.get('ID') != 'debian' or os_release.get('VERSION_ID', '').strip('"') != '12' or platform.libc_ver() != ('glibc', '2.36'):
    raise SystemExit('OCI userspace mismatch')
lib = Path(sysconfig.get_config_var('LIBDIR')) / sysconfig.get_config_var('LDLIBRARY')
def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
if sha(sys.executable) != '837a48d7afd08447fb43e28b282026fadb59368620072e57e01f66d12977e5ce' or sha(lib) != '57a3c0505572977cba024791b79fb07b787d761438befba3685339c81b5e07e6':
    raise SystemExit('OCI interpreter binary identity mismatch')
print(json.dumps(dict(runtime_os=expected_os, runtime_version=expected_version, python=platform.python_version(), host_image_os=os.environ.get('ImageOS'), host_image_version=os.environ.get('ImageVersion')), sort_keys=True))
'''
ast.parse(guard)
(OUT/'runtime-guard.py').write_text(guard)
guardstep='''      - name: Verify exact OCI runtime before candidate import
        env:
          C6_RUNNER_IMAGE_OS: ${{ inputs.runner_image_os }}
          C6_RUNNER_IMAGE_VERSION: ${{ inputs.runner_image_version }}
          C6_PYTHON_VERSION: ${{ inputs.python_version }}
        run: |
          python -I -S - <<'PY'
'''+''.join('          '+line+'\n' for line in guard.splitlines())+"          PY\n"
containerfields=f'''    container:
      image: {IMAGE}
    defaults:
      run:
        shell: bash
'''
for job,next_job in [('parallel_l1','run'),('run','handoff')]:
 a=bound.index('\n  '+job+':')+1; b=bound.index('\n  '+next_job+':',a)+1
 text=bound[a:b]
 text=replace_once(text,'    runs-on: ubuntu-24.04\n','    runs-on: ubuntu-24.04\n'+containerfields)
 text=replace_once(text,'    steps:\n','    steps:\n'+guardstep)
 text=replace_once(text,'          test "${ImageOS:-}" = "$C6_RUNNER_IMAGE_OS"\n          test "${ImageVersion:-}" = "$C6_RUNNER_IMAGE_VERSION"\n','')
 setup='''      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with:
          python-version: ${{ inputs.python_version }}
          cache: pip
          cache-dependency-path: source/requirements-lock.txt
'''
 text=replace_once(text,setup,'')
 if job=='parallel_l1':
  lo=text.index('      - name: Recover exact Base22 shard bytes\n'); hi=text.index('      - name: Compute exact chunk-preserving L1 shard\n',lo)
  text=text[:lo]+text[hi:]
  text=replace_once(text,"        if: inputs.binding_id == 'c6.base_plus_s.l1'\n",'')
  text=replace_once(text,"          if [[ \"$C6_BINDING_ID\" = 'c6.base.l1' ]]; then\n            shard_source='addb5c8ebf98ac43e08676cbc6ffe81a2627d9d7'\n          else\n            shard_source=\"$C6_SOURCE_REVISION\"\n          fi\n",'          shard_source="$C6_SOURCE_REVISION"\n')
 bound=bound[:a]+text+bound[b:]
# The legacy explicit handoff was already disabled for v29 by a v23 anchor.
# Keep it unchanged; existing main workflow_run and schedules own continuation.
parsed=yaml.safe_load(bound)
for j in ['parallel_l1','run']:
 assert parsed['jobs'][j]['container']['image']==IMAGE
 assert parsed['jobs'][j]['defaults']['run']['shell']=='bash'
 assert parsed['jobs'][j]['steps'][0]['name']=='Verify exact OCI runtime before candidate import'
 assert not any('setup-python' in s.get('uses','') for s in parsed['jobs'][j]['steps'])
assert parsed['jobs']['parallel_l1']['strategy']==yaml.safe_load(original)['jobs']['parallel_l1']['strategy']
assert 'Recover exact Base22 shard bytes' not in bound and '34128460919' not in bound
assert '!cancelled()' in parsed['jobs']['run']['if']
for job in parsed['jobs'].values():
 for step in job.get('steps',[]):
  if 'run' in step:
   p=subprocess.run(['bash','-n'],input=step['run'],text=True,capture_output=True); assert p.returncode==0,p.stderr
   m=re.search(r"(?:python\d?[^\n]*<<'PY'\n)(.*)\nPY\n?",step['run'],re.S)
   if m:ast.parse(m[1])
dispatch=git('show',W29+':.github/workflows/c6-dispatch.yml')
dispatch=replace_once(dispatch,'expected_workflow_ref = "codex/c6-v29-workflow-anchor"','expected_workflow_ref = "codex/c6-v30-workflow-anchor"')
(OUT/'c6-bound-economic.yml').write_text(bound);(OUT/'c6-dispatch.yml').write_text(dispatch)
for name,text in [('c6-bound-economic.yml',bound),('c6-dispatch.yml',dispatch)]:
 print(name,'sha256',hashlib.sha256(text.encode()).hexdigest(),'blob',hashlib.sha1(b'blob '+str(len(text.encode())).encode()+b'\0'+text.encode()).hexdigest())
print('validated structure, preserved matrix/status gates and syntax')
