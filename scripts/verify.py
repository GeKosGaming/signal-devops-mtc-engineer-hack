#!/usr/bin/env python3
"""Fail-closed verification: Gateway -> HTTP -> Prometheus -> Fluentd -> Loki."""
from __future__ import annotations
import argparse, contextlib, json, time, uuid
import ipaddress
from pathlib import Path
from runtime import *

REQUIRED_JOBS = {'prometheus', 'loki', 'nginx', 'envoy', 'node', 'gateway-probe'}

def hello(result: dict, release: str | None = None) -> dict:
    require(result['status'] == 200 and result['body'] == 'Hello World!\n', f'Unexpected HTTP response: {result}')
    if release: require(result['headers'].get('x-release') == release, 'Wrong backend release')
    return result

def targets(port: int, *, canary_after: float | None = None) -> dict:
    data = api(port, '/api/v1/targets')['data']['activeTargets']
    found = {x.get('labels',{}).get('job') for x in data}
    require(REQUIRED_JOBS <= found, f'Missing scrape jobs: {REQUIRED_JOBS-found}')
    selected = [x for x in data if x.get('labels',{}).get('job') in REQUIRED_JOBS]
    require(all(x.get('health') == 'up' for x in selected), 'Some required targets are down')
    for target in selected:
        scraped=dt.datetime.fromisoformat(target['lastScrape'].replace('Z','+00:00'))
        age=(dt.datetime.now(dt.timezone.utc)-scraped).total_seconds()
        require(0<=age<=60, f'Stale target scrape ({age:.1f}s): {target["labels"]}')
        if canary_after is not None and target['labels'].get('job') == 'nginx' and target['labels'].get('release') == 'canary':
            require(scraped.timestamp() > canary_after, 'Canary has not been scraped after this cohort')
    counts = {j: sum(x['labels']['job'] == j for x in selected) for j in REQUIRED_JOBS}
    require(counts['nginx'] >= 3 and counts['envoy'] >= 2 and counts['node'] >= 1, f'Unexpected target cardinality: {counts}')
    releases = {r: sum(x['labels'].get('job') == 'nginx' and x['labels'].get('release') == r
                       for x in selected) for r in ('stable', 'canary')}
    require(releases['stable'] >= 2 and releases['canary'] >= 1, f'Missing release-specific Nginx targets: {releases}')
    return {'counts': counts, 'releases': releases, 'targets': [{'labels': x['labels'], 'health': x['health'],
                'lastScrape': x['lastScrape'], 'lastError': x.get('lastError'), 'scrapeUrl': x.get('scrapeUrl')} for x in selected]}

def external_entrypoints() -> dict:
    profile = (ROOT/'.state/profile').read_text().strip()
    require(profile in ('kind', 'kubeadm'), 'Unknown cluster profile')
    ip = (ROOT/'.state/node-ip').read_text().strip()
    ipaddress.ip_address(ip)
    # infra/kind.yaml maps 30080 -> 8080 and 30443 -> 8443 on loopback.
    http_port, https_port = (8080, 8443) if profile == 'kind' else (30080, 30443)
    address = f'[{ip}]' if ':' in ip else ip
    return {'http_url': f'http://{address}:{http_port}/',
            'https_url': f'https://signal.local:{https_port}/', 'connect_ip': ip}

def external_http() -> dict:
    entry = external_entrypoints()
    return {'url': entry['http_url'], **hello(http(entry['http_url'], headers={'Host': 'signal.local'}), 'stable')}

def external_https() -> dict:
    entry = external_entrypoints()
    return {'url': entry['https_url'], 'connect_ip': entry['connect_ip'],
            **hello(http(entry['https_url'], headers={'Host': 'signal.local'},
                         ca=ROOT/'.state/tls/ca.crt', connect_ip=entry['connect_ip']), 'stable')}

def logs(port: int, proof: str, stream: str, start: float) -> dict:
    selector = '{namespace="signal",app="web",stream="' + stream + '"}'
    expr = selector + (' | json | proof_id="' + proof + '"' if stream=='stdout' else ' |= "' + proof + '"')
    data = api(port, '/loki/api/v1/query_range', {'query': expr, 'start': str(int((start-5)*1e9)),
                 'end': str(time.time_ns()), 'limit': '100', 'direction': 'forward'})
    result = data['data']['result']
    require(any(x.get('values') for x in result), f'No {stream} log matching proof {proof}')
    if stream == 'stdout':
        decoded = [json.loads(line) for x in result for _,line in x['values']]
        require(any(x.get('proof_id') == proof and x.get('status') == 200 and x.get('release') == 'stable' for x in decoded),
                'Loki returned lines but no matching successful stable access log')
    return {'query': expr, 'result': result}

def network_policy() -> dict:
    """Positive control through Gateway before asserting direct Pod access is blocked."""
    from render import images, sec
    name = 'policy-proof-' + uuid.uuid4().hex[:10]
    ip = get('service', 'web-stable', APP)['spec']['clusterIP']
    gateway_ip = get('service', 'signal-gateway', EDGE)['spec']['clusterIP']
    script = (f'wget -T 5 -qO- --header="Host: signal.local" http://{gateway_ip}/ | grep -qx "Hello World!"; '
              f'if wget -T 3 -qO- http://{ip}:8080/; then echo DIRECT_ACCESS_UNEXPECTED; exit 11; '
              'else echo DIRECT_ACCESS_BLOCKED; fi')
    job = {'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':'signal-audit'},
           'spec':{'backoffLimit':0,'activeDeadlineSeconds':60,'template':{'spec':{
               'automountServiceAccountToken':False,'restartPolicy':'Never','securityContext':sec(),
               'containers':[{'name':'probe','image':images()['busybox'],'command':['sh','-ec',script],
                'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,
                                   'capabilities':{'drop':['ALL']}},
                'resources':{'requests':{'cpu':'10m','memory':'16Mi'},'limits':{'cpu':'100m','memory':'32Mi'}}}]}}}}
    try:
        k('apply','-f','-',data=job)
        k('-n','signal-audit','wait','--for=condition=complete',f'job/{name}','--timeout=75s',timeout=80)
        text = k('-n','signal-audit','logs',f'job/{name}')
        require('DIRECT_ACCESS_BLOCKED' in text and 'DIRECT_ACCESS_UNEXPECTED' not in text, 'Policy proof incomplete')
        # Retest the same Service from the allowed gateway path to avoid claiming a dead backend is isolation.
        return {'job':name,'positive_control':'Gateway access succeeded before blocked direct Service access','log':text}
    finally:
        k('-n','signal-audit','delete','job',name,'--ignore-not-found','--wait=false')

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',default='evidence/runtime')
    parser.add_argument('--skip-policy',action='store_true',help='Diagnostic only; not accepted by make acceptance')
    args = parser.parse_args()
    report = Report('Full runtime verification', args.out)
    try:
        report.check('cluster version', lambda: get_version())
        report.check('GatewayClass accepted/current', lambda: conditions(get('gatewayclass','signal'), ('Accepted',)))
        report.check('Gateway accepted/programmed/current', lambda: retry(lambda: conditions(get('gateway','signal',APP),('Accepted','Programmed')),60))
        report.check('both HTTPRoutes attached, current generation', lambda: retry(routes_ready,60))
        with contextlib.ExitStack() as stack:
            hp = stack.enter_context(forward(EDGE,'signal-gateway',80))
            tp = stack.enter_context(forward(EDGE,'signal-gateway',443))
            pp = stack.enter_context(forward(OBS,'prometheus',9090))
            lp = stack.enter_context(forward(OBS,'loki',3100))
            base = f'http://127.0.0.1:{hp}'
            request = lambda path='/', extra=None: http(base+path,headers={'Host':'signal.local',**(extra or {})})
            report.check('HTTP exact response via Envoy Gateway',lambda: retry(lambda:hello(request(),'stable'),60))
            report.check('TLS validated with local CA, never insecure',lambda:hello(http(f'https://signal.local:{tp}/',headers={'Host':'signal.local'},ca=ROOT/'.state/tls/ca.crt',connect_ip='127.0.0.1'),'stable'))
            report.check('path routing /canary',lambda:hello(request('/canary'),'canary'))
            report.check('path routing /stable',lambda:hello(request('/stable'),'stable'))
            report.check('header routing',lambda:hello(request('/',{'X-Release':'canary'}),'canary'))
            def negative_host():
                r=http(base+'/',headers={'Host':'not-signal.invalid'})
                require(r['status']==404,'Unknown host must not reach application'); return r
            report.check('unknown hostname rejected',negative_host)
            report.check('external HTTP NodePort entrypoint',lambda:retry(external_http,60))
            report.check('external HTTPS NodePort with verified CA and SNI',lambda:retry(external_https,60))
            report.check('all required Prometheus targets actually up',lambda:retry(lambda:targets(pp),150))
            def metric_proof():
                before=vector(query(pp,'sum(nginx_http_requests_total)'))[0]
                for _ in range(40): hello(request(),'stable')
                def increased():
                    raw=query(pp,'sum(nginx_http_requests_total)'); after=vector(raw)[0]
                    require(after-before>=40,'Request counter has not increased by the generated load')
                    return {'before':before,'after':after,'sent':40,'query':raw,
                            'note':'stub_status also counts exporter scrapes; no claim of exclusive causal attribution'}
                return retry(increased,60)
            report.check('real request counter increase',metric_proof)
            def metrics():
                exprs=['probe_success{job="gateway-probe"}','node_cpu_seconds_total{mode="idle"}',
                       'node_memory_MemTotal_bytes','nginx_up','envoy_cluster_upstream_rq_time_count']
                result={}
                for expr in exprs:
                    raw=query(pp,expr); v=vector(raw)
                    if expr.startswith(('probe_success','nginx_up')): require(all(x==1 for x in v),'Health metric is not 1')
                    result[expr]=raw
                return result
            report.check('nonempty finite infrastructure and latency metrics',lambda:retry(metrics,90))
            proof='signal-'+uuid.uuid4().hex; started=time.time()
            def correlated_request():
                r=hello(request('/',{'X-Proof-ID':proof}),'stable')
                require(r['headers'].get('x-proof-id')==proof,'Proof ID not returned');return {'proof_id':proof,**r}
            report.check('unique request proof issued',correlated_request)
            report.check('same request found in Fluentd -> Loki access logs',lambda:retry(lambda:logs(lp,proof,'stdout',started),150))
            error_proof='error-'+uuid.uuid4().hex
            def missing():
                r=request('/missing/'+error_proof);require(r['status']==404,'Expected real missing-file 404');return r
            report.check('real application error generated',missing)
            report.check('error log collected by Fluentd -> Loki',lambda:retry(lambda:logs(lp,error_proof,'stderr',started),150))
            if not args.skip_policy:
                report.check('NetworkPolicy positive and negative controls',network_policy)
                report.check('application still healthy after isolation test',lambda:hello(request(),'stable'))
            else:
                report.check('full security check required', lambda: require(False,'--skip-policy is diagnostic only'))
        report.check('runtime image inventory',image_inventory)
    except Exception as exc:
        report.check('verification infrastructure',lambda:require(False,str(exc)))
    return 0 if report.save() else 1

def get_version() -> dict:
    d=k('get','--raw=/version'); require(d.get('major')=='1' and d.get('minor','').rstrip('+')=='35','Expected Kubernetes 1.35.x');return d

def image_inventory() -> list:
    pods=k('get','pods','-A','-o','json')['items']
    return [{'namespace':p['metadata']['namespace'],'pod':p['metadata']['name'],
             'images':[{'name':x['name'],'image':x.get('image'),'imageID':x.get('imageID'),'ready':x.get('ready')}
                       for x in p.get('status',{}).get('containerStatuses',[])]}
             for p in pods if p['metadata']['namespace'] in (APP,OBS,EDGE,'signal-logging','signal-system','kube-system')]

if __name__=='__main__': raise SystemExit(main())
