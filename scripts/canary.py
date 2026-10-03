#!/usr/bin/env python3
"""Small, observable canary controller for the demo, not a replacement for Argo Rollouts."""
from __future__ import annotations
import argparse, contextlib, json, os, signal, sys, time, uuid
from runtime import *
from verify import hello, targets
from operations import operation_lock, OperationLockError


def assess(samples: list[dict], *, minimum: int = 20, max_error_rate: float = 0.01, max_p95_ms: float = 500) -> dict:
    require(len(samples)>=minimum, f'Insufficient samples: {len(samples)} < {minimum}')
    failures=sum(x['status']!=200 or x['body']!='Hello World!\n' or x['headers'].get('x-release')!='canary' for x in samples)
    error_rate=failures/len(samples); p95=percentile([x['latency_ms'] for x in samples],0.95)
    result={'samples':len(samples),'errors':failures,'error_rate':error_rate,'p95_ms':p95,
            'max_error_rate':max_error_rate,'max_p95_ms':max_p95_ms}
    require(error_rate<=max_error_rate and p95<=max_p95_ms, 'Canary gate rejected: '+json.dumps(result))
    return result


def require_stable_baseline(refs: list[dict]) -> None:
    """Gateway API defaults group/kind on readback; compare Service references semantically."""
    message = 'Run make deploy to restore the stable baseline before canary operations'
    require(isinstance(refs, list) and len(refs) == 2, message)
    allowed = {'name', 'port', 'weight', 'group', 'kind', 'namespace'}
    require(all(isinstance(ref, dict) and set(ref) <= allowed for ref in refs), message)
    expected = {'web-stable': 100, 'web-canary': 0}
    require({ref.get('name') for ref in refs} == set(expected), message)
    for ref in refs:
        require(ref.get('group', '') == '' and ref.get('kind', 'Service') == 'Service'
                and ref.get('namespace', APP) == APP and ref.get('port') == 8080
                and ref.get('weight', 1) == expected[ref['name']], message)


def routing_split(base: str, canary: int, *, samples: int | None = None) -> dict:
    """Exercise the weighted root route; each request opens a new HTTP connection."""
    require(0 <= canary <= 100, 'Invalid canary percentage')
    n = samples if samples is not None else (40 if canary in (0, 100) else 200)
    require(n >= 20, 'Too few traffic-split samples')
    counts = {'stable': 0, 'canary': 0}
    for _ in range(n):
        result = hello(http(base+'/', headers={'Host': 'signal.local'}))
        release = result['headers'].get('x-release')
        require(release in counts, 'Unknown backend on the weighted root route')
        counts[release] += 1
    expected, actual = canary/100, counts['canary']/n
    tolerance = 0 if canary in (0, 100) else max(.04, 4*math.sqrt(expected*(1-expected)/n))
    low, high = max(.01, expected-tolerance), min(.99, expected+tolerance)
    valid = actual == expected if canary in (0, 100) else low <= actual <= high
    require(valid, f'Weighted root route does not match {canary}% canary: {counts}')
    return {'path': '/', 'samples': n, 'counts': counts, 'expected_canary_fraction': expected,
            'actual_canary_fraction': actual, 'tolerance': tolerance,
            'note': 'Finite independent-request smoke sample; four-sigma tolerance is not a distribution guarantee.'}


def fresh_canary_metrics(pp: int, after: float) -> dict:
    inventory = targets(pp, canary_after=after)
    expected = {x['labels'].get('pod') for x in inventory['targets']
                if x['labels'].get('job') == 'nginx' and x['labels'].get('release') == 'canary'}
    require(expected and None not in expected, 'Canary target has no Pod identity')
    selector = 'nginx_up{job="nginx",release="canary"}'
    health, freshness = query(pp, selector), query(pp, 'timestamp('+selector+')')
    require(all(v == 1 for v in vector(health)), 'Canary exporter reports an unhealthy backend')
    require(all(v > after and 0 <= time.time()-v <= 60 for v in vector(freshness)),
            'Canary metric predates the cohort or is stale')
    for raw in (health, freshness):
        rows = raw['data']['result']
        require({x['metric'].get('pod') for x in rows} == expected and len(rows) == len(expected),
                'Canary metric cardinality does not match discovered Pods')
        require(all(x['metric'].get('release') == 'canary' and x['metric'].get('job') == 'nginx' for x in rows),
                'Canary metric has the wrong release or job')
    return {'after_unix': after, 'targets': inventory, 'health': health, 'sample_timestamps': freshness}


@contextlib.contextmanager
def controlled_interruptions(report: Report, *, cleanup: bool = False):
    """Handle INT/TERM; during bounded cleanup record further signals without aborting it."""
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    def interrupted(number, frame):
        name = signal.Signals(number).name
        if getattr(report, '_canary_committed', False):
            # The final outcome is durable and cleanup is complete. A signal while
            # closing forwards must not turn a completed promotion into a failure.
            print(f'{name} received after canary operation committed', flush=True)
            return
        report.data.setdefault('interruptions', []).append(
            {'signal_number': number, 'signal_name': name, 'during_cleanup': cleanup})
        if not cleanup:
            raise KeyboardInterrupt(f'{name} received; attempting canary restoration')
    try:
        for number in previous: signal.signal(number, interrupted)
        yield
    finally:
        for number, handler in previous.items(): signal.signal(number, handler)


def begin_recovery(report: Report, *, demo: bool) -> Path:
    marker = ROOT/'.state/canary-restore.json'
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open('x', encoding='utf-8') as handle:
            json.dump({'operation': 'fault demo' if demo else 'healthy promotion',
                       'restore': 'make deploy && make verify',
                       'route': {'stable': 100, 'canary': 0}, 'canary_configuration': 'healthy'}, handle)
            handle.flush(); os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise CheckError('Unresolved canary recovery marker: '+str(marker)+
                         '. Restore and verify the baseline before removing this marker.') from exc
    report.data['recovery_marker'] = {'path': str(marker), 'removed': False}
    return marker


def clear_recovery(report: Report, marker: Path) -> dict:
    marker.unlink()
    report.data['recovery_marker']['removed'] = True
    return report.data['recovery_marker']


def restore_baseline(base: str, report: Report, *, restore_canary: bool = False) -> bool:
    """Attempt every cleanup step while forwards and the operation lock remain alive."""
    restoration = {'passed': False, 'restore_canary_configuration': restore_canary}
    report.data['canary_restoration'] = restoration
    with controlled_interruptions(report, cleanup=True):
        first = len(report.data['checks'])
        if restore_canary:
            def restore_config():
                inject(False)
                return {'configuration': 'healthy'}
            report.check('restore healthy canary manifests', restore_config)
        report.check('rollback to stable 100%', lambda: weights(0))
        report.check('stable root traffic after rollback', lambda:retry(lambda:routing_split(base,0),30,1))
        if restore_canary:
            report.check('canary configuration restored', lambda:observe_canary(base,report,broken=False))
        checks = report.data['checks'][first:]
        restoration['checks'] = checks
        restoration['passed'] = len(checks) == (4 if restore_canary else 2) and all(x['passed'] for x in checks)
    return restoration['passed']


def finalize_report(base: str, report: Report) -> int:
    """Reconcile signals during marker removal/report writes before committing the outcome."""
    def reconcile():
        interrupted = bool(report.data.get('interruptions'))
        failed = any(not x['passed'] for x in report.data['checks'])
        if (interrupted or failed) and 'canary_restoration' not in report.data:
            # Normal promotion may have removed its marker before this late signal.
            # Recreate recovery evidence before attempting the final rollback.
            marker = ROOT/'.state/canary-restore.json'
            if not marker.exists():
                report.check('late interruption recovery marker', lambda:begin_recovery(report,demo=False))
            if restore_baseline(base,report) and marker.exists():
                report.check('late interruption recovery marker cleared',lambda:clear_recovery(report,marker))
        if interrupted and not any(x['name'] == 'controlled interruption' for x in report.data['checks']):
            report.check('controlled interruption',lambda:require(False,'Canary operation interrupted: '+json.dumps(report.data['interruptions'])))

    with controlled_interruptions(report, cleanup=True):
        reconcile()
        try:
            passed = report.save()
        except (Exception, KeyboardInterrupt) as exc:
            report.check('canary report finalization',lambda:require(False,str(exc) or 'KeyboardInterrupt'))
            passed = False
        # A handler invoked during save may have recorded a new interruption after
        # the report calculated "passed". Keep forwards alive, restore, then rewrite.
        if ((report.data.get('interruptions') and
             not any(x['name'] == 'controlled interruption' for x in report.data['checks'])) or
            (not passed and 'canary_restoration' not in report.data)):
            reconcile()
            try:
                passed = report.save()
            except (Exception, KeyboardInterrupt) as exc:
                print('Cannot save final canary report: '+(str(exc) or 'KeyboardInterrupt'),file=sys.stderr)
                passed = False
        # This is the commit point: marker handling, any required restoration and
        # the final report write are complete. Block actual OS signal delivery while
        # making this last decision; pending signals are then post-commit signals.
        numbers = (signal.SIGINT,signal.SIGTERM)
        while True:
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK,numbers) if hasattr(signal,'pthread_sigmask') else None
            try:
                unreported = (report.data.get('interruptions') and
                              not any(x['name'] == 'controlled interruption' for x in report.data['checks']))
                if not unreported:
                    report._canary_committed = True
                    return 0 if passed and not report.data.get('interruptions') else 1
            finally:
                if blocked is not None: signal.pthread_sigmask(signal.SIG_SETMASK,blocked)
            # At most one additional reconciliation: it adds the failed interruption
            # check, so further signals cannot leave an unsafe successful promotion.
            reconcile()
            try:
                passed = report.save()
            except (Exception, KeyboardInterrupt) as exc:
                print('Cannot save final canary report: '+(str(exc) or 'KeyboardInterrupt'),file=sys.stderr)
                passed = False


def promote(base: str, pp: int, report: Report, *, max_p95_ms: float=500) -> bool:
    """On any failed gate, route back to stable and verify a real request, not just a patch."""
    require_stable_baseline(get('httproute','web-main',APP)['spec']['rules'][0]['backendRefs'])
    try:
        retry(lambda:targets(pp),90)
        for weight in (10,25,50,100):
            weights(weight)
            proof='canary-'+uuid.uuid4().hex
            cohort=[http(base+'/canary',headers={'Host':'signal.local','X-Proof-ID':proof}) for _ in range(30)]
            measurement=assess(cohort,max_p95_ms=max_p95_ms)
            completed = time.time()
            split = retry(lambda:routing_split(base, weight), 60, 1)
            telemetry = retry(lambda:fresh_canary_metrics(pp, completed), 90, 2)
            report.data['checks'].append({'name':f'canary stage {weight}%','passed':True,
                                          'evidence':{**measurement,'cohort_proof_id':proof,
                                                      'weighted_root_route': split, 'fresh_canary_metrics': telemetry}})
        return True
    except KeyboardInterrupt:
        restore_baseline(base, report)
        raise
    except Exception as exc:
        report.data['gate_rejection']=str(exc)
        restore_baseline(base, report)
        return False


def split_proof(base: str, report: Report | None = None) -> dict:
    try:
        weights(10)
        return retry(lambda:routing_split(base,10,samples=400),60,1)
    finally:
        with controlled_interruptions(report, cleanup=True) if report is not None else contextlib.nullcontext():
            weights(0)
            retry(lambda:routing_split(base,0),30,1)


def inject(broken: bool) -> None:
    from render import apps
    selected=[x for x in apps(broken) if x['metadata']['name']=='web-canary' and x['kind'] in ('ConfigMap','Deployment')]
    k('apply','--server-side','--field-manager=signal','-f','-',data={'apiVersion':'v1','kind':'List','items':selected})
    k('-n',APP,'rollout','status','deployment/web-canary','--timeout=180s',timeout=190)


def observe_canary(base: str, report: Report, *, broken: bool, timeout: float = 60) -> dict:
    """A completed rollout can precede Envoy endpoint convergence; record real attempts."""
    observation = {'expected': 'injected failure' if broken else 'healthy canary', 'attempts': []}
    report.data.setdefault('canary_data_plane_transitions', []).append(observation)
    started = time.monotonic()
    def attempt():
        item = {'attempt': len(observation['attempts']) + 1}
        observation['attempts'].append(item)
        try:
            result = http(base+'/canary', headers={'Host': 'signal.local'})
            item['response'] = result
            if broken:
                require(result['status'] == 503 and result['body'] == 'Injected canary failure\n'
                        and result['headers'].get('x-release') == 'canary',
                        'Injected fault has not reached the data plane: '+json.dumps(result))
            else:
                hello(result, 'canary')
            return result
        except (CheckError, OSError, ValueError) as exc:
            item['error'] = str(exc)
            raise
    try:
        observation['response'] = retry(attempt, timeout, 1)
        observation['passed'] = True
        return observation
    except (CheckError, OSError, ValueError) as exc:
        observation.update({'passed': False, 'error': str(exc)})
        raise
    finally:
        observation['elapsed_seconds'] = round(time.monotonic()-started, 3)


def recovery(base: str) -> dict:
    """Deletes one Pod only in this demo namespace; measures failures rather than promising zero."""
    pods=get('pods',ns=APP)['items']
    selected=[p for p in pods if p['metadata'].get('labels',{}).get('app.kubernetes.io/name')=='web-stable' and
              all(c.get('ready') for c in p.get('status',{}).get('containerStatuses',[]))]
    require(len(selected)>=2,'Two ready stable Pods are required for the recovery drill')
    old=selected[0]['metadata']['uid']; name=selected[0]['metadata']['name']; start=time.monotonic()
    k('-n',APP,'delete','pod',name,'--wait=false')
    failed,requests=0,0
    for _ in range(50):
        requests+=1
        try: hello(http(base+'/',headers={'Host':'signal.local'}),'stable')
        except (CheckError,OSError): failed+=1
        time.sleep(.1)
    k('-n',APP,'rollout','status','deployment/web-stable','--timeout=180s',timeout=190)
    new=get('pods',ns=APP)['items']
    require(old not in [x['metadata']['uid'] for x in new],'Deleted Pod has not terminated')
    hello(http(base+'/',headers={'Host':'signal.local'}),'stable')
    return {'deleted_pod':name,'requests':requests,'failed_requests':failed,'elapsed_seconds':round(time.monotonic()-start,3),
            'interpretation':'Measured recovery on one node, not node-level high availability.'}


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--demo',action='store_true',help='Inject a deliberate HTTP 503 in the canary only, then restore it')
    p.add_argument('--p95-ms',type=float,default=500)
    args=p.parse_args()
    try:
        with operation_lock():
            report=Report('Canary failure drill' if args.demo else 'Canary promotion','evidence/demo' if args.demo else 'evidence/canary')
            with controlled_interruptions(report):
                try:
                    with contextlib.ExitStack() as stack:
                        hp=stack.enter_context(forward(EDGE,'signal-gateway',80));pp=stack.enter_context(forward(OBS,'prometheus',9090))
                        base=f'http://127.0.0.1:{hp}'
                        require_stable_baseline(get('httproute','web-main',APP)['spec']['rules'][0]['backendRefs'])
                        marker, accepted, restored = None, False, False
                        try:
                            # Finish marker creation before a signal can transfer control to cleanup.
                            with controlled_interruptions(report, cleanup=True):
                                marker = begin_recovery(report, demo=args.demo)
                            require(not report.data.get('interruptions'), 'Interrupted before canary mutation')
                            if args.demo:
                                report.check('90/10 weighted routing',lambda:split_proof(base,report))
                                inject(True)
                                observed=report.check('injected fault observed through Envoy Gateway',
                                                      lambda:observe_canary(base,report,broken=True))
                                require(observed is not None, 'Fault injection did not reach the data plane within the deadline')
                                accepted=promote(base,pp,report,max_p95_ms=args.p95_ms)
                                report.check('bad canary was rejected',lambda:require(not accepted and report.data.get('gate_rejection','').startswith('Canary gate rejected:'),'Expected HTTP canary gate rejection did not occur'))
                            else:
                                accepted=promote(base,pp,report,max_p95_ms=args.p95_ms)
                                report.check('promotion completed',lambda:require(accepted,'Canary promotion rejected; stable rollback was attempted'))
                        finally:
                            if marker is not None:
                                if args.demo:
                                    restored = restore_baseline(base,report,restore_canary=True)
                                elif not accepted or report.data.get('interruptions'):
                                    restoration = report.data.get('canary_restoration')
                                    restored = restoration['passed'] if restoration is not None else restore_baseline(base,report)
                                else:
                                    # Successful healthy promotion intentionally retains 100% canary.
                                    restored = True
                                if restored:
                                    with controlled_interruptions(report, cleanup=True):
                                        report.check('recovery marker cleared',lambda:clear_recovery(report,marker))
                        if args.demo and restored:
                            report.check('single-Pod recovery measured',lambda:recovery(base))
                        return finalize_report(base,report)
                except (Exception, KeyboardInterrupt) as exc:
                    report.check('canary execution',lambda:require(False,str(exc) or 'KeyboardInterrupt'))
                with controlled_interruptions(report, cleanup=True):
                    if report.data.get('interruptions'):
                        report.check('controlled interruption',lambda:require(False,'Canary operation interrupted: '+json.dumps(report.data['interruptions'])))
                    return 0 if report.save() else 1
    except OperationLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1

if __name__=='__main__': raise SystemExit(main())
