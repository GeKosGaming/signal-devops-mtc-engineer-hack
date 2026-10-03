from __future__ import annotations
import copy, importlib.util, json, math, pathlib, re, sys, unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import render as r
import runtime
import canary

class Manifests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=r.render()['items'];cls.boot=r.render('bootstrap','demo-node')['items'];cls.all=cls.app+cls.boot
    def one(self,kind,name):
        return next(x for x in self.all if x['kind']==kind and x['metadata']['name']==name)
    def test_deterministic(self): self.assertEqual(r.render(),r.render())
    def test_unique_identities(self):
        keys=[(x['apiVersion'],x['kind'],x['metadata'].get('namespace'),x['metadata']['name']) for x in self.all]
        self.assertEqual(len(keys),len(set(keys)))
    def test_valid_api_envelopes(self):
        for x in self.all:
            self.assertTrue(x['apiVersion']);self.assertTrue(x['metadata']['name']);self.assertTrue(x['kind'])
    def test_no_plain_secrets(self): self.assertFalse(any(x['kind']=='Secret' for x in self.all))
    def test_backend_services_exist(self):
        services={x['metadata']['name'] for x in self.app if x['kind']=='Service'}
        for route in (x for x in self.app if x['kind']=='HTTPRoute'):
            for rule in route['spec']['rules']:
                for ref in rule['backendRefs']: self.assertIn(ref['name'],services);self.assertEqual(ref['port'],8080)
    def test_stable_baseline(self):
        refs=self.one('HTTPRoute','web-main')['spec']['rules'][0]['backendRefs']
        self.assertEqual([x['weight'] for x in refs],[100,0])
    def test_two_listener_parents(self):
        for name in ['web-main','web-preview']:
            self.assertEqual({x['sectionName'] for x in self.one('HTTPRoute',name)['spec']['parentRefs']},{'http','https'})
    def test_tls_termination(self):
        g=self.one('Gateway','signal')['spec'];self.assertEqual(g['listeners'][1]['tls']['mode'],'Terminate')
        self.assertEqual(g['listeners'][1]['tls']['certificateRefs'][0]['name'],'signal-tls')
    def test_named_gateway_nodeports(self):
        s=self.one('EnvoyProxy','signal')['spec']['provider']['kubernetes']['envoyService']
        self.assertEqual(s['name'],'signal-gateway');self.assertEqual(s['type'],'NodePort')
        self.assertEqual({x['nodePort'] for x in s['patch']['value']['spec']['ports']},{30080,30443})
    def test_dont_override_envoy_minor(self):
        self.assertNotIn('image',self.one('EnvoyProxy','signal')['spec']['provider']['kubernetes']['envoyDeployment']['container'])
    def test_stable_replicas_and_pdb(self):
        self.assertEqual(self.one('Deployment','web-stable')['spec']['replicas'],2)
        self.assertEqual(self.one('PodDisruptionBudget','web-stable')['spec']['minAvailable'],1)
    def test_all_workloads_have_requests_and_limits(self):
        for x in self.app:
            if x['kind'] not in ('Deployment','DaemonSet'):continue
            for c in x['spec']['template']['spec']['containers']:
                self.assertIn('memory',c['resources']['limits']);self.assertIn('cpu',c['resources']['requests'])
    def test_workload_security(self):
        for x in self.app:
            if x['kind'] not in ('Deployment','DaemonSet'):continue
            spec=x['spec']['template']['spec']
            if x['metadata']['name']!='fluentd': self.assertTrue(spec['securityContext']['runAsNonRoot'])
            self.assertEqual(spec['securityContext']['seccompProfile']['type'],'RuntimeDefault')
            for c in spec['containers']:
                self.assertFalse(c['securityContext']['allowPrivilegeEscalation'])
                self.assertTrue(c['securityContext']['readOnlyRootFilesystem'])
                self.assertEqual(c['securityContext']['capabilities']['drop'],['ALL'])
    def test_host_logs_read_only(self):
        ds=self.one('DaemonSet','fluentd')['spec']['template']['spec']
        self.assertFalse(ds['automountServiceAccountToken'])
        mounts=ds['containers'][0]['volumeMounts'];self.assertTrue(next(x for x in mounts if x['name']=='logs')['readOnly'])
        self.assertFalse(next(x for x in mounts if x['name']=='buffer')['readOnly'])
    def test_hostpath_only_explicit_exceptions(self):
        for x in self.app:
            if x['kind'] not in ('Deployment','DaemonSet'):continue
            for v in x['spec']['template']['spec']['volumes']:
                if 'hostPath' in v: self.assertIn(x['metadata']['namespace'],['signal-system','signal-logging'])
    def test_state_persistent_and_retained(self):
        for name in ('loki','prometheus','grafana','alertmanager'):
            pv=self.one('PersistentVolume','signal-'+name)
            self.assertEqual(pv['spec']['persistentVolumeReclaimPolicy'],'Retain')
            self.assertEqual(pv['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]['matchExpressions'][0]['values'],['demo-node'])
            d=self.one('Deployment',name)['spec'];self.assertEqual(d['strategy']['type'],'Recreate')
    def test_services_select_matching_pods(self):
        podspecs=[x['spec']['template'] for x in self.app if x['kind'] in ('Deployment','DaemonSet')]
        for s in [x for x in self.app if x['kind']=='Service']:
            sel=s['spec']['selector']
            self.assertTrue(any(all(p['metadata']['labels'].get(k)==v for k,v in sel.items()) for p in podspecs))
    def test_prometheus_no_secret_or_clusterwide_read(self):
        role=self.one('ClusterRole','signal-prometheus-discovery')
        self.assertEqual(role['rules'],[{'apiGroups':[''],'resources':['pods'],'verbs':['get','list','watch']}])
        self.assertFalse(any(x['kind']=='ClusterRoleBinding' for x in self.app))
    def test_expected_scrape_jobs(self):
        jobs={x['job_name']:x for x in r.prom_config()['scrape_configs']}
        self.assertEqual(set(jobs),{'prometheus','nginx','envoy','node','loki','gateway-probe'})
        self.assertEqual(jobs['envoy']['metrics_path'],'/stats/prometheus')
        self.assertIn('signal-gateway.envoy-gateway-system',jobs['gateway-probe']['static_configs'][0]['targets'][0])
    def test_envoy_discovery_excludes_controller_same_port(self):
        job=next(x for x in r.prom_config()['scrape_configs'] if x['job_name']=='envoy')
        self.assertTrue(any('owning_gateway_name' in str(x) and x.get('action')=='keep' for x in job['relabel_configs']))
    def test_config_changes_restart_only_canary(self):
        normal={(x['kind'],x['metadata']['name']):x for x in self.app}
        changed={(x['kind'],x['metadata']['name']):x for x in r.render(broken_canary=True)['items']}
        diffs=[k for k in normal if normal[k]!=changed[k]]
        self.assertEqual(set(diffs),{('Deployment','web-canary'),('ConfigMap','web-canary')})
    def test_fluentd_reads_only_application(self):
        conf=r.read('fluent.conf');self.assertIn('path /var/log/containers/*_signal_nginx-*.log',conf)
        self.assertIn('pos_file /buffers/containers.pos',conf);self.assertIn('overflow_action block',conf)
        label=conf.split('<label>')[1].split('</label>')[0];self.assertNotIn('proof_id',label)
    def test_access_log_redacts_sensitive_fields(self):
        conf=r.read('nginx.conf')
        for key in ('$http_authorization','$http_cookie','$remote_addr','$request_uri'):self.assertNotIn(key,conf)
        self.assertIn('escape=json',conf);self.assertIn('~^[a-zA-Z0-9-]{1,64}$',conf)
    def test_fault_keeps_probe_alive(self):
        c=r.nginx_config('canary',True);self.assertIn('return 503',c);self.assertIn('return 200 "ok',c)
    def test_namespaces_are_enforced(self):
        for name in ['signal','signal-observe','signal-audit']:
            self.assertEqual(self.one('Namespace',name)['metadata']['labels']['pod-security.kubernetes.io/enforce'],'restricted')
    def test_networkpolicy_default_deny(self):
        policy=self.one('NetworkPolicy','web-default-deny')['spec']
        self.assertEqual(policy['policyTypes'],['Ingress','Egress']);self.assertEqual(policy['podSelector'],{})
    def test_images_versioned(self):
        for image in r.images().values():self.assertNotIn(':latest',image);self.assertRegex(image,r':[^/]+$')
    def test_generated_snapshots_match(self):
        self.assertEqual(json.loads((ROOT/'manifests/application.json').read_text()),r.render())
        self.assertEqual(json.loads((ROOT/'manifests/bootstrap.example.json').read_text()),r.render('bootstrap'))
    def test_invalid_render_input_rejected(self):
        for node in ('','bad node','../escape'):
            with self.assertRaises(ValueError):r.render('bootstrap',node)
        with self.assertRaises(ValueError):r.render('unknown')

class FailClosed(unittest.TestCase):
    def vector(self,values):return {'status':'success','data':{'resultType':'vector','result':[{'value':[1,str(x)]} for x in values]}}
    def test_empty_vector_fails(self):
        with self.assertRaises(runtime.CheckError):runtime.vector(self.vector([]))
    def test_nan_fails(self):
        with self.assertRaises(runtime.CheckError):runtime.vector(self.vector(['NaN']))
    def test_inf_fails(self):
        with self.assertRaises(runtime.CheckError):runtime.vector(self.vector(['+Inf']))
    def test_valid_metric(self):self.assertEqual(runtime.vector(self.vector([1,2])),[1.,2.])
    def test_stale_condition_fails(self):
        obj={'metadata':{'generation':2},'status':{'conditions':[{'type':'Accepted','status':'True','observedGeneration':1}]}}
        with self.assertRaises(runtime.CheckError):runtime.conditions(obj,('Accepted',))
    def test_false_condition_fails(self):
        obj={'metadata':{'generation':2},'status':{'conditions':[{'type':'Accepted','status':'False','observedGeneration':2}]}}
        with self.assertRaises(runtime.CheckError):runtime.conditions(obj,('Accepted',))
    def test_missing_condition_fails(self):
        with self.assertRaises(runtime.CheckError):runtime.conditions({'metadata':{'generation':1}},('Accepted',))
    def test_percentile_nearest_rank(self):self.assertEqual(runtime.percentile(list(range(1,101)),.95),95)
    def test_canary_insufficient_samples_fails(self):
        with self.assertRaises(runtime.CheckError):canary.assess([])
    def sample(self,code=200,latency=3):return {'status':code,'body':'Hello World!\n','headers':{'x-release':'canary'},'latency_ms':latency}
    def test_healthy_canary_passes(self):self.assertEqual(canary.assess([self.sample() for _ in range(30)])['errors'],0)
    def test_failed_canary_fails(self):
        with self.assertRaises(runtime.CheckError):canary.assess([self.sample(503) for _ in range(30)])
    def test_slow_canary_fails(self):
        with self.assertRaises(runtime.CheckError):canary.assess([self.sample(latency=700) for _ in range(30)])
    def test_wrong_release_fails(self):
        samples=[{**self.sample(),'headers':{'x-release':'stable'}} for _ in range(30)]
        with self.assertRaises(runtime.CheckError):canary.assess(samples)
    def test_retry_does_not_hide_failure(self):
        with self.assertRaises(runtime.CheckError):runtime.retry(lambda:runtime.require(False,'deliberate failure'),timeout=0)

if __name__=='__main__':unittest.main()
