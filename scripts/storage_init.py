#!/usr/bin/env python3
"""Apply storage resources and safely reconcile the one-shot directory preparation Job.

The caller holds the deployment lock. Only an owned, terminal Job may be deleted;
PV/PVC resources and the contents of their directories are never deleted here.
"""
from __future__ import annotations
import argparse
import copy
import json
import re
import subprocess
import time
from fractions import Fraction
from pathlib import Path

NAME = 'signal-storage-init'
NAMESPACE = 'signal-system'
OWNER = 'signal.devops/managed-storage-init'
LABELS = {'app.kubernetes.io/name': NAME, 'app.kubernetes.io/part-of': 'signal'}
QUANTITY = re.compile(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+))(Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE]|[eE][+-]?\d+)?')
DECIMAL_SCALES = {'n': -9, 'u': -6, 'm': -3, '': 0, 'k': 3, 'M': 6,
                  'G': 9, 'T': 12, 'P': 15, 'E': 18}


def quantity_value(value: object) -> object:
    """Compare exact Quantity values, independent of API serialization suffixes.

    Kubernetes serializes e.g. 1000m as 1. Unsupported values remain unchanged;
    do not approximate or round resource changes when comparing Job templates.
    """
    if not isinstance(value, str) or len(value) > 64:
        return value
    match = QUANTITY.fullmatch(value)
    if not match:
        return value
    number, suffix = match.groups()
    suffix = suffix or ''
    if suffix in DECIMAL_SCALES:
        scale = DECIMAL_SCALES[suffix]
        multiplier = Fraction(10) ** scale
    elif suffix.endswith('i'):
        multiplier = Fraction(1024) ** (('Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei').index(suffix) + 1)
    else:
        scale = int(suffix[1:])
        if abs(scale) > 30:
            return value
        multiplier = Fraction(10) ** scale
    return Fraction(number) * multiplier


class StorageError(RuntimeError):
    pass


def kubectl(*args: str, data: dict | None = None) -> str:
    p = subprocess.run(['kubectl', *args], input=None if data is None else json.dumps(data),
                       text=True, capture_output=True, timeout=180)
    if p.returncode:
        raise StorageError(p.stderr.strip()[-2500:] or 'kubectl failed')
    return p.stdout


def get_job() -> dict | None:
    raw = kubectl('-n', NAMESPACE, 'get', 'job', NAME, '--ignore-not-found', '--show-managed-fields=true', '-o', 'json')
    return json.loads(raw) if raw.strip() else None


def owned(job: dict) -> bool:
    meta = job.get('metadata', {})
    labels = meta.get('labels', {})
    if meta.get('name') != NAME or meta.get('namespace') != NAMESPACE:
        return False
    if any(labels.get(key) != value for key, value in LABELS.items()):
        return False
    # Accept legacy Jobs produced by the original server-side apply. Labels alone
    # are insufficient evidence of ownership of a pre-existing resource.
    managers = {item.get('manager') for item in meta.get('managedFields', [])}
    return meta.get('annotations', {}).get(OWNER) == 'true' or bool(managers & {'signal', 'signal-storage'})


def canonical_template(job: dict, *, ignore_image_digest: bool = False) -> dict:
    template = copy.deepcopy(job['spec']['template'])
    meta = template.setdefault('metadata', {})
    for key in ('creationTimestamp', 'managedFields', 'resourceVersion', 'uid', 'generation'):
        meta.pop(key, None)
    labels = meta.setdefault('labels', {})
    for key in ('batch.kubernetes.io/controller-uid', 'batch.kubernetes.io/job-name', 'controller-uid', 'job-name'):
        labels.pop(key, None)
    if not meta.get('annotations'):
        meta.pop('annotations', None)
    spec = template['spec']
    defaults = {'dnsPolicy': 'ClusterFirst', 'schedulerName': 'default-scheduler',
                'enableServiceLinks': True, 'terminationGracePeriodSeconds': 30,
                'serviceAccountName': 'default', 'serviceAccount': 'default',
                'preemptionPolicy': 'PreemptLowerPriority'}
    for key, value in defaults.items():
        if spec.get(key) == value:
            spec.pop(key)
    for container in [*spec['containers'], *spec.get('initContainers', [])]:
        for key, value in {'terminationMessagePath': '/dev/termination-log', 'terminationMessagePolicy': 'File'}.items():
            if container.get(key) == value:
                container.pop(key)
        if ignore_image_digest:
            container['image'] = container['image'].split('@', 1)[0]
        resources = container.get('resources', {})
        for field in ('limits', 'requests'):
            if field in resources:
                resources[field] = {name: quantity_value(value) for name, value in resources[field].items()}
    return template


def plan(existing: dict | None, desired: dict) -> str:
    if existing is None:
        return 'create'
    if not owned(existing):
        raise StorageError('Refusing to modify a foreign signal-storage-init Job; ownership could not be established.')
    status = existing.get('status', {})
    conditions = {item['type'] for item in status.get('conditions', []) if item.get('status') == 'True'}
    terminal = bool(conditions & {'Complete', 'Failed'}) and not status.get('active', 0)
    if existing.get('metadata', {}).get('deletionTimestamp'):
        raise StorageError('Storage Job is already terminating; wait for deletion and rerun deployment.')
    exact = canonical_template(existing) == canonical_template(desired)
    if not terminal:
        if not exact:
            raise StorageError('An active storage Job has an incompatible template; it will not be interrupted or replaced.')
        return 'wait'
    # A completed directory preparation does not need to run again solely because
    # the same image tag has now been locked to a digest. Keep its UID unchanged.
    completed_equivalent = canonical_template(existing, ignore_image_digest=True) == canonical_template(desired, ignore_image_digest=True)
    if 'Complete' in conditions and 'Failed' not in conditions and completed_equivalent:
        return 'keep'
    return 'replace'


def delete_terminal(existing: dict) -> None:
    meta = existing['metadata']
    uid, version = meta.get('uid'), meta.get('resourceVersion')
    if not uid or not version:
        raise StorageError('Refusing deletion without the Job UID and resourceVersion.')
    # Server-side preconditions prevent deletion if the object was replaced or
    # changed since the read. Foreground deletion waits for its old Pods as well.
    kubectl('delete', f'--raw=/apis/batch/v1/namespaces/{NAMESPACE}/jobs/{NAME}', '-f', '-', data={
        'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Foreground',
        'preconditions': {'uid': uid, 'resourceVersion': version}})
    deadline = time.monotonic() + 90
    while True:
        current = get_job()
        if current is None:
            return
        if current.get('metadata', {}).get('uid') != uid:
            raise StorageError('Another storage Job appeared while waiting for deletion; refusing to modify it.')
        if time.monotonic() >= deadline:
            raise StorageError('Storage Job deletion did not finish; wait for its old Pods and rerun deployment.')
        time.sleep(2)


def reconcile(bootstrap: dict) -> str:
    jobs = [obj for obj in bootstrap['items'] if obj.get('kind') == 'Job']
    if len(jobs) != 1 or jobs[0]['metadata'].get('name') != NAME or jobs[0]['metadata'].get('namespace') != NAMESPACE:
        raise StorageError('Bootstrap must contain exactly the SIGNAL storage preparation Job.')
    desired = copy.deepcopy(jobs[0])
    desired['metadata'].setdefault('annotations', {})[OWNER] = 'true'
    existing = get_job()
    action = plan(existing, desired)  # Refuse a foreign/incompatible active Job before mutations.
    resources = {**bootstrap, 'items': [obj for obj in bootstrap['items'] if obj.get('kind') != 'Job']}
    kubectl('apply', '--server-side', '--field-manager=signal', '-f', '-', data=resources)
    if action == 'replace':
        delete_terminal(existing)
    if action in ('create', 'replace'):
        # create (rather than apply) refuses an unexpected concurrent replacement.
        kubectl('create', '--field-manager=signal-storage', '-f', '-', data=desired)
    if action != 'keep':
        kubectl('-n', NAMESPACE, 'wait', '--for=condition=complete', f'job/{NAME}', '--timeout=150s')
    return action


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bootstrap', type=Path)
    args = parser.parse_args()
    action = reconcile(json.loads(args.bootstrap.read_text()))
    print(f'Storage directory preparation: {action}; persistent volumes and data preserved.')


if __name__ == '__main__':
    main()
