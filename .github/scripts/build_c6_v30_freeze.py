"""Bounded C6 v30 runtime recovery; no strategy execution and no old imports."""
from __future__ import annotations
import argparse, base64, copy, hashlib, json, os, subprocess, sys, tempfile, urllib.error, urllib.request
from pathlib import Path

P29='8eaca66fc1f617e2a7f52caa422f99d6c3a8729f'
IB29='dfed1e6584b0b0e039bd720ff6eff32348123eab'
IS29='60d42b3931ccc7319a7cc815d363b9950e2586d7'
R29='9e7e77c37dcf8a1531c223238bfc48cdcfd45ee4'
W29='01fb58613b44aee921c43291fe56203e76b52b0c'
MAIN='ae977737b11aff0054371984dd348e5f2db9d458'
HEAD='fa1317916e47b41c27ad60e08134df06c0a2526d'
BASE='3b4b9c2554b2ac54d065173d3889040a30bc6e89'
TIME='2026-09-09T01:20:00Z'
IMAGE='python@sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970'
RUNTIME_OS='oci-debian12-amd64'
RUNTIME_VERSION='sha256-581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970'
P_PATH='artifacts/diagnostics/c6-preregistration.json'
R_PATH='artifacts/diagnostics/c6-run-bindings.json'
ROOT=Path.cwd()
OUT=Path(os.environ.get('C6_BUILD_OUTPUT', str(ROOT.parent/'c6-v30-build-output'))).resolve()
OUT.mkdir(parents=True, exist_ok=True)
ENV={**os.environ, 'GIT_AUTHOR_NAME':'C6 Runtime Recovery', 'GIT_AUTHOR_EMAIL':'c6-recovery@users.noreply.github.com', 'GIT_COMMITTER_NAME':'C6 Runtime Recovery', 'GIT_COMMITTER_EMAIL':'c6-recovery@users.noreply.github.com', 'GIT_AUTHOR_DATE':TIME, 'GIT_COMMITTER_DATE':TIME}

def cmd(*args, cwd=ROOT, data=None, env=None):
    return subprocess.check_output(args, cwd=cwd, input=data, env=env or ENV)
def git(*args, **kw):
    return cmd('git', *args, **kw)
def oid(*args, **kw):
    return git(*args, **kw).decode().strip()
def encode(value):
    return (json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False)+'\n').encode()
def sha(data):
    return hashlib.sha256(data).hexdigest()
def replace_version(value):
    if isinstance(value,dict):return {k:replace_version(v) for k,v in value.items()}
    if isinstance(value,list):return [replace_version(v) for v in value]
    return value.replace('v29','v30') if isinstance(value,str) else value

def change_tree(tree, entries):
    fd,index=tempfile.mkstemp(prefix='c6-index-');os.close(fd);os.unlink(index)
    env={**ENV,'GIT_INDEX_FILE':index}
    try:
        git('read-tree',tree,env=env)
        for path,blob in entries.items():git('update-index','--add','--cacheinfo',f'100644,{blob},{path}',env=env)
        return oid('write-tree',env=env)
    finally:
        if Path(index).exists():Path(index).unlink()
def blob(data):return oid('hash-object','-w','--stdin',data=data)
def commit(tree,parent,message):return oid('commit-tree',tree,'-p',parent,data=(message+'\n').encode())
def api(path):
    request=urllib.request.Request('https://api.github.com/repos/ychenracing/trade/'+path, headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],'Accept':'application/vnd.github+json','User-Agent':'c6-runtime-recovery'})
    try:
        with urllib.request.urlopen(request,timeout=60) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code==404:return None
        raise

def verify_live():
    expected={'main':MAIN,'codex/c6-causal-risk-closure-v11':HEAD,'codex/c6-preregistration-v29':P29,'codex/c6-base-v29':IB29,'codex/c6-s-v29':IS29,'codex/c6-evidence-v29':R29,'codex/c6-v29-workflow-anchor':W29}
    for ref,expected_sha in expected.items():
        actual=api('git/ref/heads/'+ref)
        if not actual or actual['object']['sha']!=expected_sha:raise RuntimeError('live ref changed: '+ref)
    ci=api('actions/runs/34296808809')
    assert ci['head_sha']==HEAD and ci['status']=='completed' and ci['conclusion']=='success'
    failure=api('actions/runs/34280658425')
    assert failure['head_sha']==W29 and failure['status']=='completed' and failure['conclusion']=='failure' and failure['run_attempt']==1
    proof=api('actions/runs/34297934892')
    assert proof['head_sha']=='24e0700cb40d21e8c23ac2132f2287600a8df08b' and proof['status']=='completed' and proof['conclusion']=='success'
    artifact=api('actions/artifacts/10083860114')
    assert artifact['expired'] is False and artifact['digest']=='sha256:a55fd1f700e797f325fde8c4a381a4b4f118426ba1555cfef27d6ba6561327cf'
    assert api('git/matching-refs/heads/codex/c6-dispatch/c6-v30')==[]

def native_checks(p, r, identities, work_root):
    base_work=work_root/'base'; s_work=work_root/'s'
    for directory,key in [(base_work,'I_B'),(s_work,'I_S')]:
        git('worktree','add','--detach',str(directory),identities[key]['commit'])
    sys.path.insert(0,str(base_work))
    from quantfusion.application.c6_contract import load_preregistration, load_run_bindings, validate_implementation_git_proofs
    for directory in [base_work,s_work]:
        loaded=load_preregistration(directory/P_PATH, repository=directory)
        assert loaded==p
        rb=load_run_bindings(OUT/'R30.json')
        assert rb==r
        validate_implementation_git_proofs(p,r,repository=directory,bindings_revision=identities['R']['commit'])
    for directory,alias in [(base_work,'I_B'),(s_work,'I_S')]:
        common=['tests/c6_non_economic/test_c6_contract.py','tests/c6_non_economic/test_c6_parallel_l1.py','tests/c6_non_economic/test_c6_parallel_l1_single_pass.py','tests/c6_non_economic/test_c6_parallel_l1_attestation.py','tests/c6_non_economic/test_c6_parallel_l1_recovery_cli.py','tests/c6_non_economic/test_c6_bound_run.py','tests/c6_non_economic/test_c6_diagnostics.py','tests/contract/test_architecture.py::DependencyDirectionTests::test_canonical_import_graph_is_acyclic']
        paths=common if alias=='I_B' else ['tests/c6_non_economic/test_c6_s_qualification.py','tests/c6_non_economic/test_c6_early_concentration.py']
        commands=[['python','-m','pytest','-q',*paths,'--tb=short']]
        for index,args in enumerate(commands):
            args=[sys.executable,*args[1:]]
            result=subprocess.run(args,cwd=directory,env=ENV,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            (OUT/f'{alias}-native-{index}.log').write_bytes(result.stdout)
            print(result.stdout.decode()[-1000:],flush=True)
            if result.returncode:raise RuntimeError(f'{alias} native suite failed: {index}')
    for directory in [base_work,s_work]:
        assert not oid('status','--porcelain','-uall',cwd=directory)
    return base_work,s_work

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--offline',action='store_true');parser.add_argument('--publish',action='store_true');args=parser.parse_args()
    assert not(args.offline and args.publish)
    if not args.offline:verify_live()
    assert oid('rev-parse',HEAD+'^2')==MAIN
    for old in [IB29,IS29]:
        assert git('diff','--name-only',old,HEAD,'--','quantfusion','tests','data','scripts','requirements-lock.txt','requirements.txt').decode().splitlines()==([] if old==IS29 else json.loads(git('show',P29+':'+P_PATH))['implementation_freeze']['I_S_allowed_paths'])
    old_p=json.loads(git('show',P29+':'+P_PATH));old_r=json.loads(git('show',R29+':'+R_PATH))
    bound=(OUT/'c6-bound-economic.yml').read_bytes();dispatch=(OUT/'c6-dispatch.yml').read_bytes()
    assert sha(bound)=='40c9d6e1758d0567c5a490325fcd7c7ae2e46cb919d72f31c19f253355f7a1df'
    assert sha(dispatch)=='1c234971b3311cc1b7079ab1b3e6853bae382aeb33f938e5f845dfa7d320619e'
    wtree=change_tree(W29,{'.github/workflows/c6-bound-economic.yml':blob(bound),'.github/workflows/c6-dispatch.yml':blob(dispatch)})
    w=commit(wtree,W29,'Freeze C6 v30 OCI runtime and fresh core workflow; economics unchanged')
    workflow={'path':'.github/workflows/c6-bound-economic.yml','dispatch_ref':'codex/c6-v30-workflow-anchor','revision':w,'mode':'100644','git_blob':blob(bound),'sha256':sha(bound)}
    p=copy.deepcopy(old_p);p['experiment_id']='c6-causal-risk-closure-17x958-v30';p['frozen_at']=TIME
    p['purpose']='A/D runtime recovery after the hosted runner image rollout prevented Base29 completion. All economic formulas, gates and inputs remain unchanged. Use a digest-pinned OCI userspace with measured interpreter identity and fresh Base/S calculations. No old result, checkpoint or intermediate economic record is imported; prior evidence remains seen data and retained, not relabelled or treated as out-of-sample.'
    p['authority']['recovery_continuation']['v30_immutable_userspace_recovery']={'classification':'A/D_runtime_identity','prior_P':P29,'prior_I_B':IB29,'prior_I_S':IS29,'prior_R':R29,'prior_W':W29,'failed_run':34280658425,'failure':'mixed hosted images 20260831.293.1/20260907.300.1 rejected by pre-import exact guard; seven shard attestations only; no sealed Base29 result/checkpoint','runtime_preflights':[34290737379,34296463578],'oci_proof_run':34297934892,'oci_proof_artifact':10083860114,'oci_proof_sha256':'a55fd1f700e797f325fde8c4a381a4b4f118426ba1555cfef27d6ba6561327cf','image':IMAGE,'config_id':'sha256:e8565fa265f973c06f0515fac7aa9d102edae01b5677d69216900b84029c6ad0','python_binary_sha256':'837a48d7afd08447fb43e28b282026fadb59368620072e57e01f66d12977e5ce','libpython_sha256':'57a3c0505572977cba024791b79fb07b787d761438befba3685339c81b5e07e6','runtime_identity':{'runner_image_os':RUNTIME_OS,'runner_image_version':RUNTIME_VERSION,'meaning':'OCI Debian12 linux/amd64 userspace and immutable manifest digest, not a GitHub hosted ImageVersion. Host kernel and provisioning image remain audit data. Trusted workflow pins container digest and verifies OS, libc, architecture and binary hashes before candidate import.'},'old_runtime_economic_equivalence_proven':False,'prior_records_imported':0,'prior_results_imported':0,'prior_checkpoints_imported':0,'fresh_Base_core_evaluations':3825,'fresh_Base_evaluations':3831,'Base_execution_items':3875,'economic_hypotheses_changed':False,'source_code_changed':False,'dependency_lock_changed':False,'prior_evidence_validity':'Base27 and Base22 intermediate evidence remain preserved in their original environments; no automatic cross-runtime validity claim','supersedes_for_current_execution':'v29 recovered_intermediate_policy; historical sections remain forensic snapshots','PR_head':HEAD,'PR_exact_CI':34296808809}
    p['run_templates']=replace_version(p['run_templates']);p['workflow_trigger_matrix']=replace_version(p['workflow_trigger_matrix'])
    pw=p['run_templates']['workflow'];pw.update(revision=w,file_mode='100644',file_git_blob=workflow['git_blob'],file_sha256=workflow['sha256'])
    fields=p['run_templates']['R_schema']['R_workflow_identity']['field_types']
    for k in ['dispatch_ref','revision','mode','git_blob','sha256']:fields[k]='literal '+workflow[k]
    p['run_templates']['deterministic_runtime']['runner_image_os']='fixed_OCI_userspace_oci-debian12-amd64'
    p['run_templates']['deterministic_runtime']['runner_image_version']='fixed_OCI_manifest_'+RUNTIME_VERSION
    base_spec=p['run_templates']['binding_specs'][0]
    position=base_spec['argv_template'].index('--parallel-shard-source-revision')+1
    assert base_spec['argv_template'][position]=='addb5c8ebf98ac43e08676cbc6ffe81a2627d9d7'
    base_spec['argv_template'][position]='{SOURCE_REVISION}'
    changed={'experiment_id','frozen_at','purpose','authority','run_templates','workflow_trigger_matrix'}
    assert {key for key in p if p[key]!=old_p[key]}<=changed
    for key in set(p)-changed:assert p[key]==old_p[key]
    p_bytes=(json.dumps(p,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode();pblob=blob(p_bytes)
    ptree=change_tree(P29,{P_PATH:pblob});pc=commit(ptree,BASE,'Freeze C6 v30 unchanged economics with immutable OCI runtime recovery')
    ibtree=change_tree(IB29,{P_PATH:pblob});ib=commit(ibtree,pc,'Freeze C6-Base v30 unchanged source; fresh OCI economics')
    istree=change_tree(IS29,{P_PATH:pblob});is_=commit(istree,ib,'Freeze C6-Base+S v30 exact six-path candidate')
    r=replace_version(copy.deepcopy(old_r));r['P']={'commit':pc,'tree':ptree,'blob':pblob,'sha256':sha(p_bytes)};r['workflow']=workflow
    for alias,c,t,parent,pt in [('I_B',ib,ibtree,pc,ptree),('I_S',is_,istree,ib,ibtree)]:
        r['implementations'][alias].update(commit=c,tree=t,comparison_base_commit=parent,comparison_base_tree=pt)
    for record in r['binding_records']:
        implementation=r['implementations'][record['source_alias']]
        record.update(source_revision=implementation['commit'],source_tree=implementation['tree'],source_blob_identities=implementation['required_blobs'],P=r['P'],workflow=workflow)
        spec=next(v for v in p['run_templates']['binding_specs'] if v['record_id']==record['record_id'])
        record['argv']=[v.replace('{SOURCE_REVISION}',record['source_revision']) for v in spec['argv_template']]
        record['runtime']['runner_image_os']=RUNTIME_OS;record['runtime']['runner_image_version']=RUNTIME_VERSION
        record['record_signature']=sha(encode({k:v for k,v in record.items() if k!='record_signature'}))
        for key in ['resolved_inputs','evaluation_manifest_identity','scenario_manifest_identity','synthetic_control_manifest_identity','item_manifest_contract','exit_semantics']:
            assert record[key]==next(v for v in old_r['binding_records'] if v['record_id']==record['record_id'])[key]
    r_bytes=encode(r);rblob=blob(r_bytes);rtree=change_tree(ptree,{R_PATH:rblob});rc=commit(rtree,pc,'Bind seven C6 v30 stages to exact OCI workflow and fresh candidate identities')
    (OUT/'P30.json').write_bytes(p_bytes);(OUT/'R30.json').write_bytes(r_bytes)
    identities={'W':{'commit':w,'tree':wtree},'P':r['P'],'I_B':r['implementations']['I_B'],'I_S':r['implementations']['I_S'],'R':{'commit':rc,'tree':rtree,'blob':rblob,'sha256':sha(r_bytes)}}
    receipt={'kind':'c6_v30_runtime_recovery_freeze','identities':identities,'runtime':p['authority']['recovery_continuation']['v30_immutable_userspace_recovery'],'tests_passed':False,'published':False,'economic_dispatches':0,'prior_imported_records':0}
    (OUT/'c6-v30-freeze-receipt.json').write_bytes(encode(receipt))
    native_checks(p,r,identities,Path(tempfile.mkdtemp(prefix='c6-v30-native-')))
    receipt['tests_passed']=True
    receipt['verification_scope']='same native frozen contract/transport/diagnostics/parallel/Base-S test set as validated v29 freeze; full economic acceptance remains pending'
    refmap={'codex/c6-v30-workflow-anchor':w,'codex/c6-preregistration-v30':pc,'codex/c6-base-v30':ib,'codex/c6-s-v30':is_,'codex/c6-evidence-v30':rc}
    for ref,value in refmap.items():git('update-ref','refs/c6-prepare/'+ref,value)
    git('bundle','create',str(OUT/'c6-v30-candidates.bundle'),*['refs/c6-prepare/'+ref for ref in refmap])
    receipt['bundle_sha256']=sha((OUT/'c6-v30-candidates.bundle').read_bytes())
    (OUT/'c6-v30-freeze-receipt.json').write_bytes(encode(receipt))
    print(json.dumps({key:value['commit'] for key,value in identities.items()}),flush=True)
    if args.publish:
        verify_live()
        auth=base64.b64encode(('x-access-token:'+os.environ['GITHUB_TOKEN']).encode()).decode()
        base_args=['git','-c','http.https://github.com/.extraheader=AUTHORIZATION: basic '+auth,'push','--atomic','origin']
        anchor=api('git/ref/heads/codex/c6-v30-workflow-anchor')
        if anchor is None:
            pushed=subprocess.run([*base_args,w+':refs/heads/codex/c6-v30-workflow-anchor'],cwd=ROOT,env=ENV,capture_output=True)
            if pushed.returncode:
                (OUT/'anchor-push-error.txt').write_bytes(pushed.stderr)
                raise RuntimeError('validated W30 object push needs existing connected-user workflow permission; four source refs not published')
        else:assert anchor['object']['sha']==w
        assert api('git/ref/heads/codex/c6-v30-workflow-anchor')['object']['sha']==w
        existing=[api('git/ref/heads/'+ref) for ref in refmap if ref!='codex/c6-v30-workflow-anchor']
        if any(existing):
            assert all(existing) and all(value['object']['sha']==refmap[ref] for value,ref in zip(existing,[ref for ref in refmap if ref!='codex/c6-v30-workflow-anchor']))
        else:
            subprocess.run([*base_args,*[value+':refs/heads/'+ref for ref,value in refmap.items() if ref!='codex/c6-v30-workflow-anchor']],cwd=ROOT,env=ENV,check=True)
        for ref,value in refmap.items():assert api('git/ref/heads/'+ref)['object']['sha']==value
        receipt['published']=True;(OUT/'c6-v30-freeze-receipt.json').write_bytes(encode(receipt))

if __name__=='__main__':main()
