#!/usr/bin/env python3
"""Deterministic Kubernetes JSON renderer; Python 3.10+, standard library only.

JSON is accepted natively by kubectl. Generated snapshots in manifests/ are for
review; config/ and this module are the source of truth. No shell interpolation
is used inside application configurations.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP, OBS, LOG, SYS, EDGE, AUDIT = ('signal', 'signal-observe', 'signal-logging',
                                  'signal-system', 'envoy-gateway-system', 'signal-audit')
Obj = dict[str, Any]

def read(name: str) -> str:
    return (ROOT / 'config' / name).read_text(encoding='utf-8')

def images() -> dict[str, str]:
    base = json.loads(read('images.json'))
    lock = ROOT / 'config/images.lock.json'
    if lock.exists():
        frozen = json.loads(lock.read_text())
        if set(frozen) != set(base):
            raise ValueError('Image lock keys do not match config/images.json')
        for key, value in frozen.items():
            if not value.startswith(base[key] + '@sha256:') or not re.search(r'@sha256:[a-f0-9]{64}$', value):
                raise ValueError(f'Stale or invalid image lock: {key}')
        return frozen
    return base

def labels(name: str, **extra: str) -> dict[str, str]:
    return {'app.kubernetes.io/name': name, 'app.kubernetes.io/part-of': 'signal', **extra}

def resource(kind: str, name: str, ns: str | None = None, api: str = 'v1', **fields: Any) -> Obj:
    meta: Obj = {'name': name, 'labels': labels(name)}
    if ns:
        meta['namespace'] = ns
    return {'apiVersion': api, 'kind': kind, 'metadata': meta, **fields}

def cm(name: str, ns: str, data: dict[str, str]) -> Obj:
    return resource('ConfigMap', name, ns, data=data)

def sha(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

def sec(uid: int = 65534) -> Obj:
    return {'runAsNonRoot': True, 'runAsUser': uid, 'runAsGroup': uid, 'fsGroup': uid,
            'seccompProfile': {'type': 'RuntimeDefault'}}

def container(name: str, image: str, port: int | None = None, *, args: list[str] | None = None,
              cpu: str = '50m', memory: str = '64Mi', limit: str = '256Mi') -> Obj:
    c: Obj = {'name': name, 'image': image, 'imagePullPolicy': 'IfNotPresent',
              'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                  'capabilities': {'drop': ['ALL']}},
              'resources': {'requests': {'cpu': cpu, 'memory': memory},
                            'limits': {'cpu': '1000m', 'memory': limit}}}
    if args is not None:
        c['args'] = args
    if port:
        c['ports'] = [{'name': 'http', 'containerPort': port}]
    return c

def probe(c: Obj, port: int, path: str) -> None:
    p = {'httpGet': {'path': path, 'port': port}, 'timeoutSeconds': 3, 'periodSeconds': 5}
    c['readinessProbe'] = {**p, 'failureThreshold': 3}
    c['livenessProbe'] = {**p, 'periodSeconds': 15, 'failureThreshold': 5}
    c['startupProbe'] = {**p, 'failureThreshold': 60}

def mount(c: Obj, name: str, path: str, sub: str | None = None, ro: bool = True) -> None:
    m: Obj = {'name': name, 'mountPath': path, 'readOnly': ro}
    if sub:
        m['subPath'] = sub
    c.setdefault('volumeMounts', []).append(m)

def deployment(name: str, ns: str, containers: list[Obj], volumes: list[Obj], *,
               uid: int = 65534, replicas: int = 1, config: Any = '', stateful: bool = False,
               extra_labels: dict[str, str] | None = None) -> Obj:
    lab = labels(name, **(extra_labels or {}))
    spec: Obj = {'replicas': replicas, 'selector': {'matchLabels': lab},
                 'strategy': {'type': 'Recreate'} if stateful else {
                     'type': 'RollingUpdate', 'rollingUpdate': {'maxUnavailable': 0, 'maxSurge': 1}},
                 'revisionHistoryLimit': 3,
                 'template': {'metadata': {'labels': lab, 'annotations': {'checksum/config': sha(config)}},
                              'spec': {'automountServiceAccountToken': False, 'securityContext': sec(uid),
                                       'terminationGracePeriodSeconds': 30,
                                       'containers': containers, 'volumes': volumes}}}
    return resource('Deployment', name, ns, 'apps/v1', spec=spec)

def service(name: str, ns: str, port: int, selector: dict[str, str] | None = None) -> Obj:
    return resource('Service', name, ns, spec={'type': 'ClusterIP', 'selector': selector or labels(name),
                    'ports': [{'name': 'http', 'port': port, 'targetPort': port}]})

def nginx_config(release: str, broken: bool = False) -> str:
    if release not in ('stable', 'canary'):
        raise ValueError('Unknown release')
    return read('nginx.conf').replace('@@RELEASE@@', release).replace('@@STATUS@@', '503' if broken else '200').replace('@@BODY@@', 'Injected canary failure' if broken else 'Hello World!')

def bootstrap(node: str) -> list[Obj]:
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}', node):
        raise ValueError('Invalid Kubernetes node hostname')
    out: list[Obj] = []
    for name, level in [(APP, 'restricted'), (OBS, 'restricted'), (LOG, 'privileged'),
                        (SYS, 'privileged'), (AUDIT, 'restricted')]:
        r = resource('Namespace', name)
        r['metadata']['labels'].update({'pod-security.kubernetes.io/enforce': level,
                                        'pod-security.kubernetes.io/enforce-version': 'v1.35'})
        out.append(r)
    out.append(resource('StorageClass', 'signal-local', api='storage.k8s.io/v1',
                        provisioner='kubernetes.io/no-provisioner', volumeBindingMode='WaitForFirstConsumer',
                        reclaimPolicy='Retain'))
    sizes = {'prometheus': ('5Gi', 65534), 'loki': ('5Gi', 10001),
             'grafana': ('1Gi', 472), 'alertmanager': ('1Gi', 65534)}
    for name, (size, _) in sizes.items():
        out.append(resource('PersistentVolume', f'signal-{name}', spec={
            'capacity': {'storage': size}, 'volumeMode': 'Filesystem', 'accessModes': ['ReadWriteOnce'],
            'persistentVolumeReclaimPolicy': 'Retain', 'storageClassName': 'signal-local',
            'local': {'path': f'/var/lib/signal-storage/{name}'},
            'claimRef': {'namespace': OBS, 'name': name},
            'nodeAffinity': {'required': {'nodeSelectorTerms': [{'matchExpressions': [
                {'key': 'kubernetes.io/hostname', 'operator': 'In', 'values': [node]}]}]}}}))
        out.append(resource('PersistentVolumeClaim', name, OBS, spec={
            'accessModes': ['ReadWriteOnce'], 'storageClassName': 'signal-local',
            'volumeName': f'signal-{name}', 'resources': {'requests': {'storage': size}}}))
    cmd = ' && '.join(f'mkdir -p /data/{n} && chmod 700 /data/{n} && chown {uid}:{uid} /data/{n}'
                    for n, (_, uid) in sizes.items())
    c = container('prepare', images()['busybox'], args=['sh', '-ec', cmd], memory='16Mi', limit='64Mi')
    c['securityContext'].update({'runAsUser': 0, 'runAsNonRoot': False,
                                 'capabilities': {'drop': ['ALL'], 'add': ['CHOWN', 'FOWNER', 'DAC_OVERRIDE']}})
    mount(c, 'data', '/data', ro=False)
    out.append(resource('Job', 'signal-storage-init', SYS, 'batch/v1', spec={
        'backoffLimit': 1, 'activeDeadlineSeconds': 120,
        'template': {'metadata': {'labels': labels('storage-init')}, 'spec': {
            'automountServiceAccountToken': False, 'restartPolicy': 'Never',
            'nodeSelector': {'kubernetes.io/hostname': node},
            'securityContext': {'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [c], 'volumes': [{'name': 'data', 'hostPath': {
                'path': '/var/lib/signal-storage', 'type': 'DirectoryOrCreate'}}]}}}))
    return out

def gateway() -> list[Obj]:
    out = [resource('GatewayClass', 'signal', api='gateway.networking.k8s.io/v1',
                    spec={'controllerName': 'gateway.envoyproxy.io/gatewayclass-controller'})]
    out.append(resource('EnvoyProxy', 'signal', APP, 'gateway.envoyproxy.io/v1alpha1', spec={
        'provider': {'type': 'Kubernetes', 'kubernetes': {
            'envoyDeployment': {'replicas': 2, 'container': {'resources': {
                'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'memory': '512Mi'}}}},
            'envoyService': {'name': 'signal-gateway', 'type': 'NodePort', 'externalTrafficPolicy': 'Cluster',
                'patch': {'type': 'StrategicMerge', 'value': {'spec': {'ports': [
                    {'port': 80, 'nodePort': 30080}, {'port': 443, 'nodePort': 30443}]}}}}}},
        'telemetry': {'metrics': {'prometheus': {'disable': False}}}}))
    listener: Obj = {'hostname': 'signal.local', 'allowedRoutes': {'namespaces': {'from': 'Same'}}}
    out.append(resource('Gateway', 'signal', APP, 'gateway.networking.k8s.io/v1', spec={
        'gatewayClassName': 'signal', 'infrastructure': {'parametersRef': {
            'group': 'gateway.envoyproxy.io', 'kind': 'EnvoyProxy', 'name': 'signal'}},
        'listeners': [{**listener, 'name': 'http', 'protocol': 'HTTP', 'port': 80},
                      {**listener, 'name': 'https', 'protocol': 'HTTPS', 'port': 443,
                       'tls': {'mode': 'Terminate', 'certificateRefs': [
                           {'group': '', 'kind': 'Secret', 'name': 'signal-tls'}]}}]}))
    parents = [{'name': 'signal', 'sectionName': 'http'}, {'name': 'signal', 'sectionName': 'https'}]
    def back(release: str, weight: int = 1) -> Obj:
        return {'name': f'web-{release}', 'port': 8080, 'weight': weight}
    out.append(resource('HTTPRoute', 'web-main', APP, 'gateway.networking.k8s.io/v1', spec={
        'parentRefs': parents, 'hostnames': ['signal.local'], 'rules': [
            {'matches': [{'path': {'type': 'PathPrefix', 'value': '/'}}],
             'timeouts': {'request': '5s', 'backendRequest': '3s'},
             'backendRefs': [back('stable', 100), back('canary', 0)]}]}))
    out.append(resource('HTTPRoute', 'web-preview', APP, 'gateway.networking.k8s.io/v1', spec={
        'parentRefs': parents, 'hostnames': ['signal.local'], 'rules': [
            {'matches': [{'path': {'type': 'PathPrefix', 'value': '/canary'}},
                         {'path': {'type': 'PathPrefix', 'value': '/'}, 'headers': [
                             {'name': 'X-Release', 'type': 'Exact', 'value': 'canary'}]}],
             'backendRefs': [back('canary')]},
            {'matches': [{'path': {'type': 'PathPrefix', 'value': '/stable'}}],
             'backendRefs': [back('stable')]}]}))
    return out

def apps(broken: bool) -> list[Obj]:
    out: list[Obj] = []
    for release, replicas in [('stable', 2), ('canary', 1)]:
        name = f'web-{release}'
        conf = nginx_config(release, broken and release == 'canary')
        out.append(cm(name, APP, {'nginx.conf': conf}))
        nginx = container('nginx', images()['nginx'], 8080, cpu='50m', memory='32Mi', limit='128Mi')
        nginx['command'] = ['nginx', '-g', 'daemon off;', '-c', '/etc/signal/nginx.conf']
        probe(nginx, 8080, '/healthz')
        nginx['lifecycle'] = {'preStop': {'exec': {'command': ['sh', '-c',
            'sleep 3; nginx -s quit -c /etc/signal/nginx.conf']}}}
        mount(nginx, 'config', '/etc/signal/nginx.conf', 'nginx.conf')
        mount(nginx, 'tmp', '/tmp', ro=False)
        exp = container('nginx-exporter', images()['nginx_exporter'], 9113,
                        args=['--nginx.scrape-uri=http://127.0.0.1:8081/stub_status'], cpu='10m',
                        memory='24Mi', limit='64Mi')
        probe(exp, 9113, '/metrics')
        dep = deployment(name, APP, [nginx, exp], [
            {'name': 'config', 'configMap': {'name': name}}, {'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}}],
            uid=101, replicas=replicas, config=conf, extra_labels={'signal/release': release, 'signal/workload': 'web'})
        dep['spec']['template']['spec']['topologySpreadConstraints'] = [{
            'maxSkew': 1, 'topologyKey': 'kubernetes.io/hostname', 'whenUnsatisfiable': 'ScheduleAnyway',
            'labelSelector': {'matchLabels': {'app.kubernetes.io/name': name}}}]
        out.extend([dep, service(name, APP, 8080, dep['spec']['selector']['matchLabels'])])
    out.append(resource('PodDisruptionBudget', 'web-stable', APP, 'policy/v1', spec={
        'minAvailable': 1, 'selector': {'matchLabels': {'app.kubernetes.io/name': 'web-stable'}}}))
    return out

def prom_config() -> dict[str, Any]:
    def podjob(name: str, ns: str, port: int, path: str = '/metrics') -> Obj:
        return {'job_name': name, 'metrics_path': path, 'kubernetes_sd_configs': [
            {'role': 'pod', 'namespaces': {'names': [ns]}}], 'relabel_configs': [
            {'source_labels': ['__meta_kubernetes_pod_container_port_number'], 'action': 'keep', 'regex': str(port)},
            {'source_labels': ['__meta_kubernetes_pod_phase'], 'action': 'keep', 'regex': 'Running'},
            *([{'source_labels': ['__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name'], 'action': 'keep', 'regex': 'signal'}] if name == 'envoy' else []),
            {'source_labels': ['__meta_kubernetes_namespace'], 'target_label': 'namespace'},
            {'source_labels': ['__meta_kubernetes_pod_name'], 'target_label': 'pod'},
            {'source_labels': ['__meta_kubernetes_pod_node_name'], 'target_label': 'node'},
            {'source_labels': ['__meta_kubernetes_pod_label_signal_release'], 'target_label': 'release'}]}
    return {'global': {'scrape_interval': '15s', 'evaluation_interval': '15s',
                       'external_labels': {'project': 'signal'}},
            'rule_files': ['/etc/prometheus/alerts.yaml'],
            'alerting': {'alertmanagers': [{'static_configs': [{'targets': ['alertmanager:9093']}]}]},
            'scrape_configs': [
                {'job_name': 'prometheus', 'static_configs': [{'targets': ['localhost:9090']}]},
                {'job_name': 'loki', 'static_configs': [{'targets': ['loki:3100']}]},
                podjob('nginx', APP, 9113), podjob('envoy', EDGE, 19001, '/stats/prometheus'),
                podjob('node', SYS, 9100),
                {'job_name': 'gateway-probe', 'metrics_path': '/probe', 'params': {'module': ['http_hello']},
                 'static_configs': [{'targets': [f'http://signal-gateway.{EDGE}.svc.cluster.local/']}],
                 'relabel_configs': [
                     {'source_labels': ['__address__'], 'target_label': '__param_target'},
                     {'source_labels': ['__param_target'], 'target_label': 'instance'},
                     {'target_label': '__address__', 'replacement': 'blackbox:9115'}]}]}

def dashboard() -> Obj:
    panels = []
    def panel(title: str, expr: str, kind: str = 'timeseries', unit: str = 'short', ds: str = 'prometheus') -> None:
        i = len(panels)
        panels.append({'id': i + 1, 'title': title, 'type': kind,
                       'gridPos': {'x': (i % 2) * 12, 'y': (i // 2) * 8, 'w': 12, 'h': 8},
                       'datasource': {'type': ds, 'uid': ds},
                       'targets': [{'refId': 'A', 'expr': expr, 'datasource': {'type': ds, 'uid': ds}}],
                       'fieldConfig': {'defaults': {'unit': unit}, 'overrides': []}, 'options': {}})
    panel('Gateway synthetic availability', 'probe_success{job="gateway-probe"}', 'stat', 'percentunit')
    panel('Requests per second by release', 'sum by (release) (rate(nginx_http_requests_total[2m]))', unit='reqps')
    panel('Node CPU utilization', '1 - avg by (node) (rate(node_cpu_seconds_total{mode="idle"}[2m]))', unit='percentunit')
    panel('Node memory utilization', '1 - node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes', unit='percentunit')
    panel('Envoy upstream latency p95 (ms)', 'histogram_quantile(0.95, sum by (le) (rate(envoy_cluster_upstream_rq_time_bucket[5m])))', unit='ms')
    panel('Required targets', 'up{job=~"nginx|envoy|node|loki"}', 'stat')
    panel('HTTP status counts from application logs', 'sum by (status) (count_over_time({namespace="signal",app="web",stream="stdout"} | json | __error__="" [5m]))', ds='loki')
    panel('Searchable access and error logs', '{namespace="signal",app="web"}', 'logs', ds='loki')
    return {'uid': 'signal-overview', 'title': 'SIGNAL / Evidence before confidence', 'schemaVersion': 39,
            'version': 1, 'refresh': '15s', 'time': {'from': 'now-15m', 'to': 'now'},
            'tags': ['signal', 'hackathon'], 'panels': panels}

def observability() -> list[Obj]:
    out = [cm('prometheus', OBS, {'prometheus.yml': json.dumps(prom_config(), indent=2), 'alerts.yaml': read('alerts.yaml')}),
           cm('loki', OBS, {'loki.yaml': read('loki.yaml')}),
           cm('blackbox', OBS, {'blackbox.yaml': read('blackbox.yaml')}),
           cm('alertmanager', OBS, {'alertmanager.yaml': read('alertmanager.yaml')})]
    grafana_data = {
        'datasources.yaml': json.dumps({'apiVersion': 1, 'datasources': [
            {'name': 'Prometheus', 'type': 'prometheus', 'uid': 'prometheus', 'url': 'http://prometheus:9090', 'access': 'proxy', 'isDefault': True},
            {'name': 'Loki', 'type': 'loki', 'uid': 'loki', 'url': 'http://loki:3100', 'access': 'proxy'}]}),
        'providers.yaml': json.dumps({'apiVersion': 1, 'providers': [{'name': 'SIGNAL', 'type': 'file', 'disableDeletion': True,
                          'options': {'path': '/etc/grafana/dashboards'}}]}),
        'signal.json': json.dumps(dashboard())}
    out.append(cm('grafana', OBS, grafana_data))
    definitions = [
        ('prometheus', 9090, 65534, '/prometheus', '/etc/prometheus',
         ['--config.file=/etc/prometheus/prometheus.yml', '--storage.tsdb.path=/prometheus',
          '--storage.tsdb.retention.time=24h', '--storage.tsdb.retention.size=3GB'], '/-/ready', '256Mi', '768Mi'),
        ('loki', 3100, 10001, '/var/loki', '/etc/loki', ['-config.file=/etc/loki/loki.yaml'], '/ready', '256Mi', '1024Mi'),
        ('alertmanager', 9093, 65534, '/alertmanager', '/etc/alertmanager',
         ['--config.file=/etc/alertmanager/alertmanager.yaml', '--storage.path=/alertmanager', '--cluster.listen-address='], '/-/ready', '32Mi', '128Mi'),
        ('grafana', 3000, 472, '/var/lib/grafana', '', [], '/api/health', '128Mi', '512Mi')]
    for name, port, uid, data_path, conf_path, args, health, mem, limit in definitions:
        c = container(name, images()[name], port, args=args, memory=mem, limit=limit)
        probe(c, port, health)
        mount(c, 'data', data_path, ro=False)
        mount(c, 'tmp', '/tmp', ro=False)
        if conf_path:
            mount(c, 'config', conf_path)
        else:
            for file, target in [('datasources.yaml', '/etc/grafana/provisioning/datasources/signal.yaml'),
                                  ('providers.yaml', '/etc/grafana/provisioning/dashboards/signal.yaml'),
                                  ('signal.json', '/etc/grafana/dashboards/signal.json')]:
                mount(c, 'config', target, file)
            envs = {'GF_SECURITY_ADMIN_USER': 'admin', 'GF_USERS_ALLOW_SIGN_UP': 'false',
                    'GF_AUTH_ANONYMOUS_ENABLED': 'false', 'GF_ANALYTICS_REPORTING_ENABLED': 'false',
                    'GF_ANALYTICS_CHECK_FOR_UPDATES': 'false', 'GF_SECURITY_DISABLE_GRAVATAR': 'true'}
            c['env'] = [{'name': k, 'value': v} for k, v in envs.items()] + [
                {'name': 'GF_SECURITY_ADMIN_PASSWORD', 'valueFrom': {'secretKeyRef': {
                    'name': 'grafana-admin', 'key': 'password'}}}]
        dep = deployment(name, OBS, [c], [{'name': 'config', 'configMap': {'name': name}},
            {'name': 'data', 'persistentVolumeClaim': {'claimName': name}},
            {'name': 'tmp', 'emptyDir': {'sizeLimit': '128Mi'}}], uid=uid, stateful=True,
            config=next(x['data'] for x in out if x['metadata']['name'] == name))
        if name == 'prometheus':
            dep['spec']['template']['spec'].update({'serviceAccountName': 'prometheus', 'automountServiceAccountToken': True})
        out.extend([dep, service(name, OBS, port)])
    bb = container('blackbox', images()['blackbox'], 9115, args=['--config.file=/etc/blackbox/blackbox.yaml'], memory='32Mi', limit='128Mi')
    probe(bb, 9115, '/metrics')
    mount(bb, 'config', '/etc/blackbox')
    out.extend([deployment('blackbox', OBS, [bb], [{'name': 'config', 'configMap': {'name': 'blackbox'}}], config=read('blackbox.yaml')),
                service('blackbox', OBS, 9115), resource('ServiceAccount', 'prometheus', OBS)])
    out.append(resource('ClusterRole', 'signal-prometheus-discovery', api='rbac.authorization.k8s.io/v1',
                        rules=[{'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list', 'watch']}]))
    # Namespaced bindings avoid granting cluster-wide pod discovery permissions.
    for ns in [APP, EDGE, SYS]:
        out.append(resource('RoleBinding', 'signal-prometheus-discovery', ns, 'rbac.authorization.k8s.io/v1',
            roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'signal-prometheus-discovery'},
            subjects=[{'kind': 'ServiceAccount', 'name': 'prometheus', 'namespace': OBS}]))
    node = container('node-exporter', images()['node_exporter'], 9100,
                     args=['--path.rootfs=/host', '--path.procfs=/host/proc', '--path.sysfs=/host/sys'],
                     cpu='20m', memory='32Mi', limit='128Mi')
    probe(node, 9100, '/metrics')
    mount(node, 'root', '/host')
    ds = deployment('node-exporter', SYS, [node], [{'name': 'root', 'hostPath': {'path': '/', 'type': 'Directory'}}])
    ds['kind'] = 'DaemonSet'
    for key in ['replicas', 'strategy']:
        ds['spec'].pop(key)
    ds['spec']['template']['spec']['tolerations'] = [{'operator': 'Exists'}]
    out.append(ds)
    return out

def logging() -> list[Obj]:
    c = container('fluentd', images()['fluentd'], 24220, memory='128Mi', limit='384Mi')
    c['command'] = ['fluentd']
    c['args'] = ['-c', '/fluentd/etc/fluent.conf', '-p', '/fluentd/plugins']
    probe(c, 24220, '/api/plugins.json')
    c.pop('livenessProbe')  # A slow sink should not turn retries into a restart loop.
    mount(c, 'config', '/fluentd/etc/fluent.conf', 'fluent.conf')
    mount(c, 'logs', '/var/log')
    mount(c, 'buffer', '/buffers', ro=False)
    mount(c, 'tmp', '/tmp', ro=False)
    dep = deployment('fluentd', LOG, [c], [
        {'name': 'config', 'configMap': {'name': 'fluentd'}},
        {'name': 'logs', 'hostPath': {'path': '/var/log', 'type': 'Directory'}},
        {'name': 'buffer', 'hostPath': {'path': '/var/lib/signal-fluentd', 'type': 'DirectoryOrCreate'}},
        {'name': 'tmp', 'emptyDir': {'sizeLimit': '32Mi'}}], uid=0, config=read('fluent.conf'))
    dep['kind'] = 'DaemonSet'
    dep['spec'].pop('replicas'); dep['spec'].pop('strategy')
    dep['spec']['template']['spec']['securityContext']['runAsNonRoot'] = False
    dep['spec']['template']['spec']['tolerations'] = [{'operator': 'Exists'}]
    return [cm('fluentd', LOG, {'fluent.conf': read('fluent.conf')}), dep]

def network_policies() -> list[Obj]:
    def ns(name: str) -> Obj:
        return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': name}}}
    def policy(name: str, namespace: str, spec: Obj) -> Obj:
        return resource('NetworkPolicy', name, namespace, 'networking.k8s.io/v1', spec=spec)
    def tcp(port: int) -> Obj:
        return {'protocol': 'TCP', 'port': port}
    return [
        policy('web-default-deny', APP, {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}),
        policy('web-from-gateway', APP, {'podSelector': {'matchLabels': {'signal/workload': 'web'}},
            'policyTypes': ['Ingress'], 'ingress': [{'from': [{**ns(EDGE), 'podSelector': {'matchLabels': {
                'gateway.envoyproxy.io/owning-gateway-name': 'signal',
                'gateway.envoyproxy.io/owning-gateway-namespace': APP}}}], 'ports': [tcp(8080)]}]}),
        policy('web-metrics', APP, {'podSelector': {'matchLabels': {'signal/workload': 'web'}},
            'policyTypes': ['Ingress'], 'ingress': [{'from': [{**ns(OBS), 'podSelector': {
                'matchLabels': {'app.kubernetes.io/name': 'prometheus'}}}], 'ports': [tcp(9113)]}]}),
        policy('observe-ingress', OBS, {'podSelector': {}, 'policyTypes': ['Ingress'],
            'ingress': [{'from': [ns(OBS)]}]}),
        policy('loki-from-fluentd', OBS, {'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'loki'}},
            'policyTypes': ['Ingress'], 'ingress': [{'from': [{**ns(LOG), 'podSelector': {
                'matchLabels': {'app.kubernetes.io/name': 'fluentd'}}}], 'ports': [tcp(3100)]}]}),
        policy('fluentd-minimum', LOG, {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
            'egress': [{'to': [ns(OBS)], 'ports': [tcp(3100)]}, {'to': [{**ns('kube-system'),
                'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                'ports': [tcp(53), {'protocol': 'UDP', 'port': 53}]}]}),
        policy('node-metrics', SYS, {'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'node-exporter'}},
            'policyTypes': ['Ingress'], 'ingress': [{'from': [{**ns(OBS), 'podSelector': {
                'matchLabels': {'app.kubernetes.io/name': 'prometheus'}}}], 'ports': [tcp(9100)]}]})]

def render(stage: str = 'app', node: str = 'signal-node', broken_canary: bool = False) -> Obj:
    if stage == 'bootstrap':
        items = bootstrap(node)
    elif stage == 'app':
        items = gateway() + apps(broken_canary) + observability() + logging() + network_policies()
    else:
        raise ValueError('Unknown stage')
    return {'apiVersion': 'v1', 'kind': 'List', 'items': items}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['bootstrap', 'app'], default='app')
    parser.add_argument('--node', default='signal-node')
    parser.add_argument('--broken-canary', action='store_true', help='Only for the explicit failure drill')
    args = parser.parse_args()
    print(json.dumps(render(args.stage, args.node, args.broken_canary), indent=2, ensure_ascii=False))

if __name__ == '__main__':
    main()
