#!/usr/bin/env python3
"""Shared runtime helpers. No external Python packages; never read Kubernetes Secrets."""
from __future__ import annotations
import contextlib, datetime as dt, html, json, math, os, socket, ssl, subprocess, time
import urllib.error, urllib.parse, urllib.request
from http.client import HTTPSConnection
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
APP, OBS, EDGE = 'signal', 'signal-observe', 'envoy-gateway-system'
CONTROLLER = 'gateway.envoyproxy.io/gatewayclass-controller'

class CheckError(RuntimeError):
    pass

def command_env() -> dict[str, str]:
    """Use this bundle's cluster without changing the caller's environment/config."""
    env = os.environ.copy()
    env['PATH'] = str(ROOT / '.tools') + os.pathsep + env.get('PATH', '')
    env['KUBECONFIG'] = str(ROOT / '.state/kubeconfig')
    return env

def run(args: list[str], *, data: Any = None, timeout: float = 90) -> str:
    from operations import lock_pass_fds
    p = subprocess.run(args, input=None if data is None else json.dumps(data), text=True,
                       capture_output=True, timeout=timeout, cwd=ROOT, env=command_env(),
                       pass_fds=lock_pass_fds(ROOT), start_new_session=os.name=='posix')
    if p.returncode:
        raise CheckError(f'{args[0]} {args[1:]}: {p.stderr.strip()[-2500:]}')
    return p.stdout

def k(*args: str, data: Any = None, timeout: float = 90) -> Any:
    result = run(['kubectl', *args], data=data, timeout=timeout)
    return json.loads(result) if result.strip().startswith(('{', '[')) else result

def get(kind: str, name: str = '', ns: str | None = None) -> dict:
    args = ['get', kind] + ([name] if name else []) + (['-n', ns] if ns else []) + ['-o', 'json']
    return k(*args)

def require(ok: Any, message: str) -> None:
    if not ok:
        raise CheckError(message)

def retry(fn: Callable[[], Any], timeout: float = 90, interval: float = 3) -> Any:
    deadline, last = time.monotonic() + timeout, 'No attempt'
    while True:
        try:
            return fn()
        except (CheckError, OSError, ValueError) as exc:
            last = str(exc)
        if time.monotonic() >= deadline:
            raise CheckError(f'Timed out after {timeout:g}s: {last}')
        time.sleep(min(interval, max(0, deadline - time.monotonic())))

@contextlib.contextmanager
def forward(ns: str, service: str, remote: int) -> Iterator[int]:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    env = command_env()
    # A read-only forwarding process must not keep a mutation lock alive if
    # its owning operator is forcibly killed.
    env.pop('SIGNAL_OPERATION_LOCK_FD', None)
    p = subprocess.Popen(['kubectl', '-n', ns, 'port-forward', '--address=127.0.0.1',
                          f'service/{service}', f'{port}:{remote}'], stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT,
                          start_new_session=os.name=='posix')
    try:
        def ready() -> bool:
            require(p.poll() is None, 'Port-forward stopped before becoming ready')
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                return True
        retry(ready, 35, 0.3)
        yield port
    finally:
        if p.poll() is None:
            p.terminate()
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=5)
        if p.stderr: p.stderr.close()

def http(url: str, *, headers: dict[str, str] | None = None,
         ca: Path | None = None, timeout: float = 8, connect_ip: str | None = None) -> dict:
    context = ssl.create_default_context(cafile=str(ca)) if ca else ssl.create_default_context()
    class ConnectedHTTPS(HTTPSConnection):
        def connect(self) -> None:
            original = self._create_connection
            if connect_ip:
                self._create_connection = lambda address, *a, **kw: original((connect_ip, address[1]), *a, **kw)
            super().connect()  # URL hostname remains the verified TLS hostname and SNI.
    class ConnectedHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(ConnectedHTTPS, req, context=context)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), ConnectedHandler(context=context))
    req = urllib.request.Request(url, headers=headers or {})
    start = time.monotonic()
    try:
        response = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        response = e
    with response:
        return {'status': response.code, 'body': response.read(2_000_000).decode('utf-8', 'replace'),
                'headers': {x.lower(): y for x, y in response.headers.items()},
                'latency_ms': round((time.monotonic() - start) * 1000, 3)}

def api(port: int, path: str, params: dict[str, str] | None = None) -> dict:
    result = http(f'http://127.0.0.1:{port}{path}' + ('?' + urllib.parse.urlencode(params) if params else ''))
    require(result['status'] == 200, f'API {path}: HTTP {result["status"]}')
    data = json.loads(result['body'])
    require(data.get('status') == 'success', f'API {path}: not a successful response')
    return data

def query(port: int, expr: str) -> dict:
    return api(port, '/api/v1/query', {'query': expr})

def vector(data: dict) -> list[float]:
    require(data.get('status') == 'success', 'Unsuccessful Prometheus query')
    require(data.get('data', {}).get('resultType') == 'vector', 'Expected instant vector')
    result = data['data']['result']
    require(bool(result), 'Empty metric vector is NOT success')
    values = [float(x['value'][1]) for x in result]
    require(all(math.isfinite(x) for x in values), 'Non-finite metric value')
    return values

def conditions(obj: dict, names: tuple[str, ...]) -> dict:
    expected = obj['metadata']['generation']
    actual = {x['type']: x for x in obj.get('status', {}).get('conditions', [])}
    for name in names:
        require(name in actual, f'Missing condition {name}')
        c = actual[name]
        require(c.get('status') == 'True', f'{name} is not True: {c}')
        require(c.get('observedGeneration') == expected, f'Stale {name} condition')
    return {name: actual[name] for name in names}

def routes_ready() -> dict:
    result = {}
    for name in ['web-main', 'web-preview']:
        r = get('httproute', name, APP)
        parents = [p for p in r.get('status', {}).get('parents', [])
                   if p.get('controllerName') == CONTROLLER and p.get('parentRef', {}).get('name') == 'signal']
        require({p['parentRef'].get('sectionName') for p in parents} >= {'http', 'https'}, f'{name}: both listeners must acknowledge route')
        result[name] = []
        for parent in parents:
            checked = conditions({'metadata': r['metadata'], 'status': parent}, ('Accepted', 'ResolvedRefs'))
            result[name].append({'listener': parent['parentRef'].get('sectionName'), 'conditions': checked})
    return result

def weights(canary: int) -> dict:
    require(0 <= canary <= 100, 'Invalid canary percentage')
    from render import gateway
    # Update and Apply are separate SSA ownership entries even with the same
    # manager name. Keep deployment and runtime route changes under one Apply.
    route = next(x for x in gateway() if x['kind'] == 'HTTPRoute' and x['metadata']['name'] == 'web-main')
    selected = {'web-stable': 100-canary, 'web-canary': canary}
    for ref in route['spec']['rules'][0]['backendRefs']:
        ref['weight'] = selected[ref['name']]
    k('apply', '--server-side', '--field-manager=signal', '-f', '-', data=route)
    return retry(routes_ready, 60, 1)

def percentile(values: list[float], p: float) -> float:
    require(bool(values), 'No latency samples')
    require(0 < p <= 1, 'Invalid percentile')
    return sorted(values)[max(0, math.ceil(len(values) * p)-1)]

def environment() -> dict:
    osrelease = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            a,b = line.split('=',1); osrelease[a] = b.strip('"')
    try: commit = run(['git', 'rev-parse', 'HEAD']).strip()
    except (CheckError, OSError): commit = 'uncommitted-local-bundle'
    try: clean = not run(['git', 'status', '--porcelain', '--untracked-files=normal']).strip()
    except (CheckError, OSError): clean = False
    return {'timestamp_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'os': osrelease,
            'git_commit': commit, 'git_worktree_clean': clean,
            'profile': (ROOT/'.state/profile').read_text().strip() if (ROOT/'.state/profile').exists() else 'unknown'}

class Report:
    def __init__(self, name: str, out: str):
        self.out = ROOT / out
        self.data = {'name': name, 'environment': environment(), 'checks': [], 'passed': False}
    def check(self, name: str, fn: Callable[[], Any]) -> Any:
        start = time.monotonic()
        try:
            evidence = fn()
            self.data['checks'].append({'name': name, 'passed': True, 'evidence': evidence,
                                       'duration_seconds': round(time.monotonic()-start,3)})
            print(f'PASS {name}', flush=True); return evidence
        except Exception as exc:
            self.data['checks'].append({'name': name, 'passed': False, 'error': str(exc),
                                       'duration_seconds': round(time.monotonic()-start,3)})
            print(f'FAIL {name}: {exc}', flush=True); return None
    def save(self) -> bool:
        self.data['passed'] = bool(self.data['checks']) and all(x['passed'] for x in self.data['checks'])
        self.out.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(self.data, ensure_ascii=False, indent=2)
        (self.out/'report.json').write_text(raw, encoding='utf-8')
        title = html.escape(self.data['name'])
        status = 'PASS' if self.data['passed'] else 'FAIL'
        rows = ''.join(f'<tr><td>{html.escape(x["name"])}</td><td class="{str(x["passed"]).lower()}">{"PASS" if x["passed"] else "FAIL"}</td><td><pre>{html.escape(json.dumps(x.get("evidence", x.get("error")),ensure_ascii=False,indent=2))}</pre></td></tr>' for x in self.data['checks'])
        page = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{title}</title><style>body{{font:16px system-ui;margin:3% auto;max-width:1200px;background:#111821;color:#e8edf3}}h1{{font-size:36px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:16px;border-bottom:1px solid #425266;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;word-break:break-word;max-height:380px;overflow:auto;font-size:12px}}.true{{color:#9de5b0}}.false{{color:#ffaba6}}small{{color:#aab5c3}}</style><h1>SIGNAL / {status}</h1><p>{title}</p><small>Measured evidence, not a readiness claim based on Pod phase. No external scripts, fonts or telemetry.</small><pre>{html.escape(json.dumps(self.data['environment'],ensure_ascii=False,indent=2))}</pre><table><tr><th>Check</th><th>Result</th><th>Evidence</th></tr>{rows}</table></html>'''
        (self.out/'report.html').write_text(page, encoding='utf-8')
        print(f'Report: {self.out}/report.html', flush=True)
        return self.data['passed']
