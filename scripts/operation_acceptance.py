#!/usr/bin/env python3
"""Prove operation contention and actual POSIX interruption on an isolated stand."""
from __future__ import annotations
import argparse, os, signal, subprocess, sys, time
from operations import operation_lock
from runtime import APP, OBS, EDGE, ROOT, Report, CheckError, command_env, forward, get, http, require, retry
from canary import inject, require_stable_baseline
from runtime import weights
from verify import hello


def baseline(base: str) -> dict:
    require_stable_baseline(get('httproute', 'web-main', APP)['spec']['rules'][0]['backendRefs'])
    return {'stable': hello(http(base+'/', headers={'Host':'signal.local'}), 'stable'),
            'canary': hello(http(base+'/canary', headers={'Host':'signal.local'}), 'canary')}


def snapshot() -> dict:
    route = get('httproute','web-main',APP)
    canary = get('deployment','web-canary',APP)
    loki = get('deployment','loki',OBS)
    return {'route_spec':route['spec'], 'canary_template':canary['spec']['template'],
            'loki_uid':loki['metadata']['uid'], 'loki_replicas':loki['spec'].get('replicas',1)}


def competing_operations() -> dict:
    results = []
    env = command_env(); env.pop('SIGNAL_OPERATION_LOCK_FD',None)
    commands = [['bash','scripts/deploy.sh'], [sys.executable,'scripts/canary.py','--demo'],
                [sys.executable,'scripts/log_delivery.py','--run'], [sys.executable,'scripts/acceptance.py']]
    with operation_lock():
        before = snapshot()
        for command in commands:
            started = time.monotonic()
            child = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                                   close_fds=True, timeout=15)
            result = {'command':command, 'returncode':child.returncode,
                      'elapsed_seconds':round(time.monotonic()-started,3),
                      'output':(child.stdout+'\n'+child.stderr)[-2000:]}
            results.append(result)
            require(child.returncode != 0 and 'another signal operation' in result['output'].lower(),
                    'A competing mutation was not rejected by the common lock: '+str(result))
        after = snapshot()
        require(before == after, 'A rejected competing command changed cluster configuration')
    return {'commands':results, 'configuration_unchanged':True}


def interrupted_canary(base: str, signum: int, demo: bool) -> dict:
    retry(lambda:baseline(base),60,1)
    marker = ROOT/'.state/canary-restore.json'
    require(not marker.exists(), 'Resolve an existing canary recovery marker before this drill')
    env = command_env(); env.pop('SIGNAL_OPERATION_LOCK_FD',None); env['PYTHONUNBUFFERED']='1'
    path = ROOT/'.state'/('operation-probe-'+signal.Signals(signum).name+'.log')
    report_path = ROOT/'evidence'/('demo' if demo else 'canary')/'report.json'
    # A previous failed drill must never substitute for this child's evidence.
    report_path.unlink(missing_ok=True)
    command = [sys.executable,'scripts/canary.py']+(['--demo'] if demo else [])
    started = time.monotonic(); sent = False
    with path.open('w',encoding='utf-8') as log:
        child = subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
                                 close_fds=True,start_new_session=True)
        try:
            deadline = time.monotonic()+180
            while True:
                require(child.poll() is None, 'Canary exited before the signal probe: '+path.read_text(encoding='utf-8')[-2000:])
                if demo:
                    response = http(base+'/canary',headers={'Host':'signal.local'})
                    mutated = (response['status']==503 and response['body']=='Injected canary failure\n'
                               and response['headers'].get('x-release')=='canary')
                else:
                    refs = get('httproute','web-main',APP)['spec']['rules'][0]['backendRefs']
                    mutated = any(x['name']=='web-canary' and 0<x.get('weight',0)<=100 for x in refs)
                if mutated and marker.exists():
                    break
                require(time.monotonic()<deadline, 'No actual mutation and recovery marker observed before deadline')
                time.sleep(.2)
            if signum==signal.SIGINT:
                os.killpg(child.pid,signum)  # Model terminal Ctrl+C for the operator's process group.
            else:
                os.kill(child.pid,signum)
            sent = True
            code = child.wait(timeout=600)
        except Exception:
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL); child.wait(timeout=15)
            with operation_lock():
                inject(False); weights(0)
            raise
        finally:
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL); child.wait(timeout=15)
    raw = path.read_text(encoding='utf-8')
    # Even a failed probe should leave the isolated lab safe for diagnostics.
    if not sent or marker.exists() or child.returncode in (-signal.SIGTERM,-signal.SIGINT):
        with operation_lock():
            inject(False); weights(0)
        raise CheckError('Interrupted process did not complete its own restoration; safety fallback ran: '+raw[-2000:])
    require(code == 1, 'Interruption must have the controlled failed exit 1, not success or signal death')
    import json
    report = json.loads(report_path.read_text(encoding='utf-8'))
    require(report['passed'] is False, 'Interrupted operation must save a failing report')
    require(any(x.get('signal_name')==signal.Signals(signum).name for x in report.get('interruptions',[])),
            'The interrupted report did not identify the actual signal')
    require(report.get('canary_restoration',{}).get('passed') is True,
            'The operation did not record its own successful restoration')
    restored = retry(lambda:baseline(base),90,1)
    require(not marker.exists(), 'Recovery marker remained after successful restoration')
    # Check that both Python and shell mutation entry points can run again.
    with operation_lock(): pass
    check = subprocess.run(['bash','-c','source scripts/lib.sh; lock'],cwd=ROOT,env=env,
                           capture_output=True,text=True,timeout=10)
    require(check.returncode==0,'Operation lock was not released after interruption')
    return {'signal':signal.Signals(signum).name,'signal_scope':'operator process group' if signum==signal.SIGINT else 'operator PID',
            'mode':'fault demo' if demo else 'healthy promotion',
            'mutation_observed_before_signal':True,'returncode':code,'saved_report_passed':False,
            'recovery_marker_removed':True,'lock_released':True,'restored':restored,
            'elapsed_seconds':round(time.monotonic()-started,3),'process_output':raw[-2000:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true'); args = parser.parse_args()
    if not args.run: parser.error('Explicit --run is required on the isolated SIGNAL stand')
    report = Report('Actual operation contention and canary signal recovery','evidence/operations')
    try:
        require(os.name=='posix','Actual signal acceptance requires the Ubuntu stand')
        with forward(EDGE,'signal-gateway',80) as port:
            base = f'http://127.0.0.1:{port}'
            report.check('safe baseline before operation probes',lambda:retry(lambda:baseline(base),60,1))
            report.check('concurrent mutations rejected before cluster changes',competing_operations)
            report.check('SIGTERM after actual faulty canary injection restores baseline',
                         lambda:interrupted_canary(base,signal.SIGTERM,True))
            report.check('SIGINT during healthy promotion restores baseline',
                         lambda:interrupted_canary(base,signal.SIGINT,False))
            report.check('stable baseline after operation probes',lambda:retry(lambda:baseline(base),60,1))
    except Exception as exc:
        report.check('operation probes execution',lambda:require(False,str(exc)))
    return 0 if report.save() else 1

if __name__=='__main__': raise SystemExit(main())
