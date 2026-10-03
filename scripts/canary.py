#!/usr/bin/env python3
"""Small, observable canary controller for the demo, not a replacement for Argo Rollouts."""
from __future__ import annotations
import argparse, contextlib, json, time, uuid
from runtime import *
from verify import hello, targets


def assess(samples: list[dict], *, minimum: int = 20, max_error_rate: float = 0.01, max_p95_ms: float = 500) -> dict:
    require(len(samples)>=minimum, f'Insufficient samples: {len(samples)} < {minimum}')
    failures=sum(x['status']!=200 or x['body']!='Hello World!\n' or x['headers'].get('x-release')!='canary' for x in samples)
    error_rate=failures/len(samples); p95=percentile([x['latency_ms'] for x in samples],0.95)
    result={'samples':len(samples),'errors':failures,'error_rate':error_rate,'p95_ms':p95,
            'max_error_rate':max_error_rate,'max_p95_ms':max_p95_ms}
    require(error_rate<=max_error_rate and p95<=max_p95_ms, 'Canary gate rejected: '+json.dumps(result))
    return result


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


def promote(base: str, pp: int, report: Report, *, max_p95_ms: float=500) -> bool:
    """On any failed gate, route back to stable and verify a real request, not just a patch."""
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
    except Exception as exc:
        report.data['gate_rejection']=str(exc)
        report.check('rollback to stable 100%', lambda: weights(0))
        report.check('stable root traffic after rollback',lambda:retry(lambda:routing_split(base,0),30,1))
        return False


def split_proof(base: str) -> dict:
    try:
        weights(10)
        return retry(lambda:routing_split(base,10,samples=400),60,1)
    finally:
        weights(0)
        retry(lambda:routing_split(base,0),30,1)


def inject(broken: bool) -> None:
    from render import apps
    selected=[x for x in apps(broken) if x['metadata']['name']=='web-canary' and x['kind'] in ('ConfigMap','Deployment')]
    k('apply','--server-side','--field-manager=signal','-f','-',data={'apiVersion':'v1','kind':'List','items':selected})
    k('-n',APP,'rollout','status','deployment/web-canary','--timeout=180s',timeout=190)


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
    report=Report('Canary failure drill' if args.demo else 'Canary promotion','evidence/demo' if args.demo else 'evidence/canary')
    try:
        with contextlib.ExitStack() as stack:
            hp=stack.enter_context(forward(EDGE,'signal-gateway',80));pp=stack.enter_context(forward(OBS,'prometheus',9090))
            base=f'http://127.0.0.1:{hp}'
            if args.demo:
                initial=get('httproute','web-main',APP)['spec']['rules'][0]['backendRefs']
                require(initial==[{'name':'web-stable','port':8080,'weight':100},{'name':'web-canary','port':8080,'weight':0}],
                        'Run make deploy to restore the baseline before the destructive demo')
                try:
                    report.check('90/10 weighted routing',lambda:split_proof(base))
                    inject(True)
                    r=http(base+'/canary',headers={'Host':'signal.local'})
                    require(r['status']==503,'Fault injection did not produce 503')
                    accepted=promote(base,pp,report,max_p95_ms=args.p95_ms)
                    report.check('bad canary was rejected',lambda:require(not accepted and report.data.get('gate_rejection','').startswith('Canary gate rejected:'),'Expected HTTP canary gate rejection did not occur'))
                finally:
                    # The intentionally modified canary is restored even if a gate, connection or assertion fails.
                    try: inject(False)
                    finally: weights(0)
                report.check('canary configuration restored',lambda:hello(http(base+'/canary',headers={'Host':'signal.local'}),'canary'))
                report.check('single-Pod recovery measured',lambda:recovery(base))
            else:
                accepted=promote(base,pp,report,max_p95_ms=args.p95_ms)
                report.check('promotion completed',lambda:require(accepted,'Canary promotion rejected; stable rollback was attempted'))
    except Exception as exc:
        report.check('canary execution',lambda:require(False,str(exc)))
    return 0 if report.save() else 1

if __name__=='__main__': raise SystemExit(main())
