#!/usr/bin/env python3
"""Explicit isolated-stand drill: pause Loki, issue real requests, measure log replay."""
from __future__ import annotations
import argparse, contextlib, json, signal, sys, time, uuid
from runtime import *
from verify import hello, logs
from operations import OperationLockError, operation_lock


def restore_loki(baseline: dict) -> None:
    current = get('deployment', 'loki', OBS)
    require(current['metadata']['uid'] == baseline['uid'], 'Loki Deployment was replaced; refuse to scale another object')
    replicas = current['spec'].get('replicas', 1)
    require(replicas in (0, baseline['replicas']), 'Loki replicas changed concurrently; restore manually using the saved baseline')
    if replicas != baseline['replicas']:
        k('-n', OBS, 'scale', 'deployment/loki', '--current-replicas=0', f'--replicas={baseline["replicas"]}')
    k('-n', OBS, 'rollout', 'status', 'deployment/loki', '--timeout=180s', timeout=190)
    restored = get('deployment', 'loki', OBS)
    require(restored['metadata']['uid'] == baseline['uid'] and restored['spec'].get('replicas', 1) == baseline['replicas'],
            'Loki baseline was not restored')


@contextlib.contextmanager
def loki_outage(report: Report):
    dep = get('deployment', 'loki', OBS)
    baseline = {'uid': dep['metadata']['uid'], 'replicas': dep['spec'].get('replicas', 1)}
    require(baseline['replicas'] >= 1, 'Loki must be running before this drill')
    marker = ROOT/'.state/log-delivery-restore.json'
    marker.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive marker prevents overlapping drills and keeps recovery data if
    # restoration fails or the process is forcibly killed.
    with marker.open('x', encoding='utf-8') as handle:
        json.dump(baseline, handle)
    report.data['loki_baseline'] = baseline
    try:
        k('-n', OBS, 'scale', 'deployment/loki', f'--current-replicas={baseline["replicas"]}', '--replicas=0')
        def stopped():
            pods = k('-n', OBS, 'get', 'pods', '-l', 'app.kubernetes.io/name=loki', '-o', 'json')['items']
            require(not pods, 'Loki Pods have not terminated yet')
            return True
        retry(stopped, 90, 1)
        report.data['loki_unavailable_verified_unix'] = time.time()
        yield baseline
    finally:
        try:
            restore_loki(baseline)
        except BaseException as exc:
            report.data['loki_restoration'] = {'passed': False, 'error': str(exc), 'baseline_file': str(marker)}
            raise
        else:
            report.data['loki_restoration'] = {'passed': True, 'baseline': baseline}
            marker.unlink()


def delivery_snapshot(port: int, prefix: str, started: float) -> list[dict]:
    expr = '{namespace="signal",app="web",stream="stdout"} | json | proof_id=~"'+prefix+'-[0-9]+"'
    raw = api(port, '/loki/api/v1/query_range', {'query': expr, 'start': str(int((started-5)*1e9)),
              'end': str(time.time_ns()), 'limit': '5000', 'direction': 'forward'})
    result = raw['data']['result']
    rows = [(stream.get('stream', {}), stamp, line) for stream in result for stamp, line in stream.get('values', [])]
    require(len(rows) < 5000, 'Loki result reached its limit; duplicate counts would be incomplete')
    records = []
    for labels, stamp, line in rows:
        item = json.loads(line)
        records.append({'labels': labels, 'timestamp_ns': stamp, 'record': item})
    return records


def analyse_delivery(records: list[dict], issued: dict[str, float], first_seen: dict[str, float], now: float) -> dict:
    counts = {proof: 0 for proof in issued}
    for row in records:
        record = row['record']; proof = record.get('proof_id')
        if proof in counts and record.get('status') == 200 and record.get('release') == 'stable':
            counts[proof] += 1
    for proof, count in counts.items():
        if count:
            first_seen.setdefault(proof, now)
    missing = [proof for proof, count in counts.items() if not count]
    duplicates = {proof: count-1 for proof, count in counts.items() if count > 1}
    return {'expected_requests': len(issued), 'delivered_requests': len(issued)-len(missing),
            'missing_proof_ids': missing, 'extra_visible_copies': duplicates, 'copies_per_proof': counts,
            'first_observed_delay_seconds': {p: round(first_seen[p]-issued[p], 3) for p in first_seen},
            'observed_until_unix': now,
            'interpretation': 'Counts are visible Loki query records within this finite window. Query polling delay is included; identical records may be deduplicated by Loki. This does not prove exactly-once delivery.'}


def observe_delivery(port: int, prefix: str, issued: dict[str, float], started: float,
                     *, timeout: float = 180, settle: float = 10) -> dict:
    deadline = time.monotonic()+timeout
    first_seen: dict[str, float] = {}
    previous, unchanged_since, latest = None, time.monotonic(), {}
    while True:
        # Once restored, query errors are real failures, not success with no data.
        records = retry(lambda: delivery_snapshot(port, prefix, started), 30, 2)
        latest = analyse_delivery(records, issued, first_seen, time.time())
        if latest['copies_per_proof'] != previous:
            previous, unchanged_since = latest['copies_per_proof'].copy(), time.monotonic()
        if not latest['missing_proof_ids'] and time.monotonic()-unchanged_since >= settle:
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(min(2, max(0, deadline-time.monotonic())))


def drill(base: str, report: Report, *, count: int, pause_seconds: float, timeout: float) -> dict:
    prefix = 'log-'+uuid.uuid4().hex
    started = time.time()
    # Prove the pipeline before the planned outage, with a separate control ID.
    proof = 'control-'+uuid.uuid4().hex
    hello(http(base+'/', headers={'Host': 'signal.local', 'X-Proof-ID': proof}), 'stable')
    with forward(OBS, 'loki', 3100) as port:
        retry(lambda:logs(port, proof, 'stdout', started), 150)
    issued: dict[str, float] = {}
    report.data['requests_during_loki_outage'] = issued
    with loki_outage(report):
        paused = time.monotonic()
        for i in range(count):
            proof = prefix+'-'+str(i)
            issued[proof] = time.time()
            result = hello(http(base+'/', headers={'Host': 'signal.local', 'X-Proof-ID': proof}), 'stable')
            require(result['headers'].get('x-proof-id') == proof, 'Request proof was not echoed')
            time.sleep(.1)
        time.sleep(max(0, pause_seconds-(time.monotonic()-paused)))
    # A new forward is necessary: the old Loki Pod was deliberately terminated.
    with forward(OBS, 'loki', 3100) as port:
        evidence = observe_delivery(port, prefix, issued, started, timeout=timeout)
    report.data['delivery_measurement'] = evidence
    require(not evidence['missing_proof_ids'], 'Some accepted HTTP requests did not become searchable before the deadline')
    return evidence


def run_drill(args: argparse.Namespace) -> int:
    report = Report('Controlled Loki outage and observed log delivery', 'evidence/log-delivery')
    previous_handler = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Terminated; attempting Loki restoration')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        with forward(EDGE, 'signal-gateway', 80) as port:
            report.check('requests logged across a controlled Loki outage',
                         lambda:drill(f'http://127.0.0.1:{port}', report, count=args.requests,
                                      pause_seconds=args.pause_seconds, timeout=args.delivery_timeout))
    except (Exception, KeyboardInterrupt) as exc:
        report.check('outage drill execution', lambda:require(False, str(exc)))
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    return 0 if report.save() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly permit scaling Loki down on this isolated stand')
    parser.add_argument('--requests', type=int, default=20)
    parser.add_argument('--pause-seconds', type=float, default=10)
    parser.add_argument('--delivery-timeout', type=float, default=180)
    args = parser.parse_args()
    if not args.run:
        parser.error('This optional outage drill requires --run on the isolated SIGNAL stand')
    if not 1 <= args.requests <= 100 or not 1 <= args.pause_seconds <= 60 or not 10 <= args.delivery_timeout <= 600:
        parser.error('Require 1..100 requests, 1..60 pause seconds and 10..600 delivery timeout seconds')
    try:
        with operation_lock(ROOT):
            return run_drill(args)
    except OperationLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
