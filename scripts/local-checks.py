#!/usr/bin/env python3
"""Run actual local checks and write their result, without suggesting a cluster was run."""
import datetime, json, os, pathlib, platform, shutil, subprocess, sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT); result={'timestamp_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
  'environment':{'platform':platform.platform(),'python':sys.version,'os_release':pathlib.Path('/etc/os-release').read_text() if pathlib.Path('/etc/os-release').exists() else None},
  'ubuntu_kubeadm_e2e':'NOT_RUN','pinned_container_images':'NOT_RUN','checks':[]}
commands=[[sys.executable,'-m','compileall','-q','scripts','tests'],[sys.executable,'-m','unittest','discover','-s','tests','-v']]
commands += [[shutil.which('bash') or 'bash','-n',str(p)] for p in sorted(pathlib.Path('scripts').glob('*.sh'))]
if shutil.which('nginx'):
 p=subprocess.run(['nginx','-v'],capture_output=True,text=True);result['environment']['local_nginx']=p.stderr.strip()
for cmd in commands:
 p=subprocess.run(cmd,capture_output=True,text=True);print(p.stdout,end='');print(p.stderr,end='',file=sys.stderr)
 result['checks'].append({'command':cmd,'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
result['passed']=all(x['returncode']==0 for x in result['checks'])
path=ROOT/'evidence/local-checks.json';path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
print('Local check report:',path)
sys.exit(0 if result['passed'] else 1)
