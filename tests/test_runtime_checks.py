from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import canary
import log_delivery as delivery
import runtime
import verify


def response(release='canary'):
    return {'status': 200, 'body': 'Hello World!\n', 'headers': {'x-release': release}, 'latency_ms': 1}


class MemoryReport:
    def __init__(self): self.data = {'checks': []}
    def check(self, name, fn):
        try:
            result = fn(); self.data['checks'].append({'name': name, 'passed': True}); return result
        except Exception as exc:
            self.data['checks'].append({'name': name, 'passed': False, 'error': str(exc)})


class ExternalTLS(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root/'.state').mkdir()
        (self.root/'.state/node-ip').write_text('127.0.0.1')
        self.scope = patch.object(verify, 'ROOT', self.root); self.scope.start(); self.addCleanup(self.scope.stop)
    def profile(self, name): (self.root/'.state/profile').write_text(name)
    def test_kind_https_uses_external_mapping_and_preserves_verified_hostname(self):
        self.profile('kind')
        with patch.object(verify, 'http', return_value=response('stable')) as request:
            verify.external_https()
        self.assertEqual(request.call_args.args, ('https://signal.local:8443/',))
        self.assertEqual(request.call_args.kwargs['connect_ip'], '127.0.0.1')
        self.assertEqual(request.call_args.kwargs['ca'], self.root/'.state/tls/ca.crt')
        self.assertEqual(request.call_args.kwargs['headers']['Host'], 'signal.local')
        mapping = (ROOT/'infra/kind.yaml').read_text()
        self.assertRegex(mapping, r'containerPort: 30443\s+hostPort: 8443')
    def test_kubeadm_uses_nodeport_and_cannot_be_replaced_by_http_override(self):
        self.profile('kubeadm')
        with patch.dict(os.environ, {'SIGNAL_HTTP_URL': 'http://wrong-backend/'}):
            result = verify.external_entrypoints()
        self.assertEqual(result['http_url'], 'http://127.0.0.1:30080/')
        self.assertEqual(result['https_url'], 'https://signal.local:30443/')
    def test_external_https_failure_is_not_hidden_by_internal_tls_success(self):
        self.profile('kind')
        with patch.object(verify, 'http', side_effect=OSError('HTTPS NodePort blocked')):
            with self.assertRaises(OSError): verify.external_https()
    def test_wrong_external_release_fails(self):
        self.profile('kind')
        with patch.object(verify, 'http', return_value=response('canary')):
            with self.assertRaises(runtime.CheckError): verify.external_https()


class ProcessIsolation(unittest.TestCase):
    def test_child_process_uses_bundle_config_without_mutating_caller(self):
        with patch.dict(os.environ, {'KUBECONFIG': 'existing-user-config', 'PATH': 'original-path'}):
            before = os.environ.copy()
            with patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='ok')) as child:
                self.assertEqual(runtime.run(['kubectl', 'get', 'nodes']), 'ok')
            self.assertEqual(os.environ, before)
        self.assertEqual(child.call_args.kwargs['env']['KUBECONFIG'], str(ROOT/'.state/kubeconfig'))
        self.assertTrue(child.call_args.kwargs['env']['PATH'].startswith(str(ROOT/'.tools')))
    def test_port_forward_also_uses_isolated_environment(self):
        process = MagicMock(); process.poll.return_value = None
        with patch.dict(os.environ, {'KUBECONFIG': 'existing-user-config'}), \
             patch.object(runtime.subprocess, 'Popen', return_value=process) as child, \
             patch.object(runtime, 'retry', return_value=True):
            with runtime.forward('signal-observe', 'loki', 3100): pass
            self.assertEqual(os.environ['KUBECONFIG'], 'existing-user-config')
        self.assertEqual(child.call_args.kwargs['env']['KUBECONFIG'], str(ROOT/'.state/kubeconfig'))
        process.terminate.assert_called_once()
    def test_dirty_worktree_cannot_be_reported_clean(self):
        fake_os = SimpleNamespace(read_text=lambda: 'ID=ubuntu\nVERSION_ID="24.04"\n')
        with patch.object(runtime, 'Path', return_value=fake_os), \
             patch.object(runtime, 'run', side_effect=['commit-id', ' M scripts/canary.py\n']):
            self.assertFalse(runtime.environment()['git_worktree_clean'])
    def test_git_status_failure_is_not_clean(self):
        fake_os = SimpleNamespace(read_text=lambda: 'ID=ubuntu\n')
        with patch.object(runtime, 'Path', return_value=fake_os), \
             patch.object(runtime, 'run', side_effect=['commit-id', runtime.CheckError('no git')]):
            self.assertFalse(runtime.environment()['git_worktree_clean'])


class CanaryGates(unittest.TestCase):
    def baseline(self):
        return [{'name': 'web-stable', 'port': 8080, 'weight': 100},
                {'name': 'web-canary', 'port': 8080, 'weight': 0}]
    def test_gateway_crd_readback_defaults_are_accepted(self):
        actual = [{**ref, 'group': '', 'kind': 'Service'} for ref in self.baseline()]
        canary.require_stable_baseline(actual)
        canary.require_stable_baseline(list(reversed(actual)))
    def test_explicit_same_namespace_is_semantically_the_baseline(self):
        canary.require_stable_baseline([{**ref, 'namespace': runtime.APP} for ref in self.baseline()])
    def test_foreign_namespace_or_group_or_kind_is_rejected(self):
        for override in ({'namespace': 'another'}, {'group': 'other.io'}, {'kind': 'CustomBackend'}):
            with self.subTest(override=override):
                refs = self.baseline(); refs[0].update(override)
                with self.assertRaises(runtime.CheckError): canary.require_stable_baseline(refs)
    def test_baseline_does_not_ignore_filters_or_changed_weights(self):
        for override in ({'filters': [{'type': 'RequestHeaderModifier'}]}, {'weight': 90}, {'name': 'other-service'}):
            with self.subTest(override=override):
                refs = self.baseline(); refs[0].update(override)
                with self.assertRaises(runtime.CheckError): canary.require_stable_baseline(refs)
    def scrape_inventory(self):
        stamp = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=1)).isoformat()
        definitions = [('prometheus', None), ('loki', None), ('gateway-probe', None), ('node', None),
                       ('envoy', None), ('envoy', None), ('nginx', 'stable'), ('nginx', 'stable'), ('nginx', 'canary')]
        rows = []
        for i, (job, release) in enumerate(definitions):
            labels = {'job': job, 'pod': str(i)}
            if release: labels['release'] = release
            rows.append({'labels': labels, 'health': 'up', 'lastScrape': stamp})
        return {'data': {'activeTargets': rows}}
    def test_total_nginx_count_cannot_mask_missing_canary_scrape(self):
        data = self.scrape_inventory(); data['data']['activeTargets'][-1]['labels']['release'] = 'stable'
        with patch.object(verify, 'api', return_value=data):
            with self.assertRaises(runtime.CheckError): verify.targets(9090)
    def test_target_scrape_must_follow_cohort_completion(self):
        data = self.scrape_inventory()
        after = dt.datetime.now(dt.timezone.utc).timestamp()
        with patch.object(verify, 'api', return_value=data):
            with self.assertRaises(runtime.CheckError): verify.targets(9090, canary_after=after)
    def test_healthy_promotion_validates_each_root_stage_including_one_hundred_percent(self):
        state = {'weight': 0}; root_counts = {}
        def weight(value): state['weight'] = value
        def request(url, **kwargs):
            if url.endswith('/canary'): return response('canary')
            current = state['weight']; seen = root_counts.get(current, 0)
            root_counts[current] = seen+1
            return response('canary' if seen % 100 < current else 'stable')
        report = MemoryReport()
        with patch.object(canary, 'targets', return_value={}), patch.object(canary, 'weights', side_effect=weight), \
             patch.object(canary, 'http', side_effect=request), \
             patch.object(canary, 'fresh_canary_metrics', return_value={'fresh': True}), \
             patch.object(canary, 'retry', side_effect=lambda fn, *a, **kw: fn()):
            self.assertTrue(canary.promote('http://gateway', 9090, report))
        self.assertEqual(root_counts, {10: 200, 25: 200, 50: 200, 100: 40})
        self.assertEqual(len(report.data['checks']), 4)
        self.assertEqual(report.data['checks'][-1]['evidence']['weighted_root_route']['counts']['stable'], 0)
    def test_split_exercises_root_and_observes_each_backend(self):
        sample = [response('canary')]*20 + [response('stable')]*180
        with patch.object(canary, 'http', side_effect=sample) as request:
            result = canary.routing_split('http://gateway', 10)
        self.assertEqual(result['counts'], {'stable': 180, 'canary': 20})
        self.assertTrue(all(call.args[0] == 'http://gateway/' for call in request.call_args_list))
    def test_healthy_direct_cohort_cannot_mask_root_stuck_on_stable(self):
        report = MemoryReport()
        with patch.object(canary, 'targets', return_value={}), patch.object(canary, 'weights'), \
             patch.object(canary, 'retry', side_effect=lambda fn, *a, **kw: fn()), \
             patch.object(canary, 'http', side_effect=lambda url, **kw: response('canary' if url.endswith('/canary') else 'stable')):
            self.assertFalse(canary.promote('http://gateway', 9090, report))
        self.assertIn('Weighted root route', report.data['gate_rejection'])
        self.assertTrue(all(item['passed'] for item in report.data['checks']))
    def test_final_stage_requires_all_root_samples_to_be_canary(self):
        with patch.object(canary, 'http', side_effect=[response('canary')]*39+[response('stable')]):
            with self.assertRaises(runtime.CheckError): canary.routing_split('http://gateway', 100)
    def inventory(self):
        return {'targets': [{'labels': {'job': 'nginx', 'release': 'canary', 'pod': 'canary-1'}}]}
    def metric(self, value, *, pod='canary-1', release='canary'):
        return {'status': 'success', 'data': {'resultType': 'vector', 'result': [
            {'metric': {'job': 'nginx', 'release': release, 'pod': pod}, 'value': [100, str(value)]}]}}
    def test_metric_from_before_cohort_cannot_pass(self):
        with patch.object(canary, 'targets', return_value=self.inventory()), \
             patch.object(canary, 'query', side_effect=[self.metric(1), self.metric(99)]):
            with self.assertRaises(runtime.CheckError): canary.fresh_canary_metrics(9090, 100)
    def test_stable_metric_cannot_substitute_for_missing_canary(self):
        with patch.object(canary, 'targets', return_value=self.inventory()), \
             patch.object(canary, 'query', side_effect=[self.metric(1, release='stable'), self.metric(101, release='stable')]), \
             patch.object(canary.time, 'time', return_value=102):
            with self.assertRaises(runtime.CheckError): canary.fresh_canary_metrics(9090, 100)
    def test_extra_stale_pod_metric_fails_cardinality(self):
        health = self.metric(1); health['data']['result'].append(self.metric(1, pod='old-canary')['data']['result'][0])
        with patch.object(canary, 'targets', return_value=self.inventory()), \
             patch.object(canary, 'query', side_effect=[health, self.metric(101)]), \
             patch.object(canary.time, 'time', return_value=102):
            with self.assertRaises(runtime.CheckError): canary.fresh_canary_metrics(9090, 100)
    def test_fresh_canary_metric_passes_and_requests_post_cohort_scrape(self):
        with patch.object(canary, 'targets', return_value=self.inventory()) as inventory, \
             patch.object(canary, 'query', side_effect=[self.metric(1), self.metric(101)]) as query, \
             patch.object(canary.time, 'time', return_value=102):
            canary.fresh_canary_metrics(9090, 100)
        inventory.assert_called_once_with(9090, canary_after=100)
        self.assertIn('timestamp(nginx_up', query.call_args.args[1])


class LogOutage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scope = patch.object(delivery, 'ROOT', self.root); scope.start(); self.addCleanup(scope.stop)
        self.dep = {'metadata': {'uid': 'original'}, 'spec': {'replicas': 2}}
        self.calls = []
    def command(self, *args, **kwargs):
        self.calls.append(args)
        for arg in args:
            if arg.startswith('--replicas='): self.dep['spec']['replicas'] = int(arg.split('=')[1])
        if 'get' in args and 'pods' in args: return {'items': []}
        return ''
    def test_baseline_restored_after_body_exception(self):
        report = MemoryReport()
        with patch.object(delivery, 'get', side_effect=lambda *a, **kw: self.dep), \
             patch.object(delivery, 'k', side_effect=self.command):
            with self.assertRaisesRegex(RuntimeError, 'request failed'):
                with delivery.loki_outage(report): raise RuntimeError('request failed')
        self.assertEqual(self.dep['spec']['replicas'], 2)
        self.assertTrue(report.data['loki_restoration']['passed'])
        self.assertFalse((self.root/'.state/log-delivery-restore.json').exists())
    def test_baseline_restored_after_shutdown_wait_failure(self):
        report = MemoryReport()
        with patch.object(delivery, 'get', side_effect=lambda *a, **kw: self.dep), \
             patch.object(delivery, 'k', side_effect=self.command), \
             patch.object(delivery, 'retry', side_effect=runtime.CheckError('Pods did not stop')):
            with self.assertRaises(runtime.CheckError):
                with delivery.loki_outage(report): self.fail('must not run requests yet')
        self.assertEqual(self.dep['spec']['replicas'], 2)
    def test_interrupt_also_restores_replicas(self):
        with patch.object(delivery, 'get', side_effect=lambda *a, **kw: self.dep), \
             patch.object(delivery, 'k', side_effect=self.command):
            with self.assertRaises(KeyboardInterrupt):
                with delivery.loki_outage(MemoryReport()): raise KeyboardInterrupt()
        self.assertEqual(self.dep['spec']['replicas'], 2)
    def test_replaced_deployment_is_not_overwritten_and_marker_is_retained(self):
        report = MemoryReport()
        with patch.object(delivery, 'get', side_effect=lambda *a, **kw: self.dep), \
             patch.object(delivery, 'k', side_effect=self.command):
            with self.assertRaisesRegex(runtime.CheckError, 'replaced'):
                with delivery.loki_outage(report): self.dep['metadata']['uid'] = 'another-deployment'
        self.assertTrue((self.root/'.state/log-delivery-restore.json').exists())
        self.assertFalse(report.data['loki_restoration']['passed'])
    def test_missing_duplicate_and_delay_are_reported_separately(self):
        rows = [{'record': {'proof_id': 'proof-1', 'status': 200, 'release': 'stable'}}]*2
        first_seen = {}
        result = delivery.analyse_delivery(rows, {'proof-1': 10, 'proof-2': 11}, first_seen, 20)
        self.assertEqual(result['missing_proof_ids'], ['proof-2'])
        self.assertEqual(result['extra_visible_copies'], {'proof-1': 1})
        self.assertEqual(result['first_observed_delay_seconds'], {'proof-1': 10})
        again = delivery.analyse_delivery(rows, {'proof-1': 10, 'proof-2': 11}, first_seen, 25)
        self.assertEqual(again['first_observed_delay_seconds'], {'proof-1': 10})
        self.assertIn('does not prove exactly-once', result['interpretation'])
    def test_empty_loki_query_does_not_become_delivery_success(self):
        with patch.object(delivery, 'delivery_snapshot', return_value=[]):
            result = delivery.observe_delivery(3100, 'session', {'proof-1': 10}, 10, timeout=0, settle=0)
        self.assertEqual(result['delivered_requests'], 0)
        self.assertEqual(result['missing_proof_ids'], ['proof-1'])


if __name__ == '__main__': unittest.main()
