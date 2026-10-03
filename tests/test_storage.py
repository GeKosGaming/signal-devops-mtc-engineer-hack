from __future__ import annotations
import copy
import json
import pathlib
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import acceptance
import render
import storage_init as storage


def desired_job() -> dict:
    return next(obj for obj in render.bootstrap('demo-node') if obj['kind'] == 'Job')


def existing_job(condition: str | None = 'Complete') -> dict:
    job = copy.deepcopy(desired_job())
    job['metadata'].update({'uid':'old-uid', 'resourceVersion':'27', 'managedFields':[{'manager':'signal'}]})
    labels = job['spec']['template']['metadata']['labels']
    labels.update({'controller-uid':'old-uid', 'batch.kubernetes.io/controller-uid':'old-uid',
                   'job-name':storage.NAME, 'batch.kubernetes.io/job-name':storage.NAME})
    job['spec']['template']['metadata']['creationTimestamp'] = None
    spec = job['spec']['template']['spec']
    spec.update({'dnsPolicy':'ClusterFirst', 'schedulerName':'default-scheduler',
                 'enableServiceLinks':True, 'terminationGracePeriodSeconds':30})
    spec['containers'][0].update({'terminationMessagePath':'/dev/termination-log', 'terminationMessagePolicy':'File'})
    # The API serializes Quantity values in canonical form (1000m becomes 1).
    spec['containers'][0]['resources']['limits']['cpu'] = '1'
    # The non-pointer VolumeMount.readOnly bool uses json omitempty.
    spec['containers'][0]['volumeMounts'][0].pop('readOnly')
    job['status'] = {'conditions':[{'type':condition, 'status':'True'}]} if condition else {'active':1}
    return job


class StorageLifecycle(unittest.TestCase):
    def test_read_includes_managed_fields_for_legacy_ownership(self):
        with patch.object(storage, 'kubectl', return_value='') as cli:
            self.assertIsNone(storage.get_job())
        self.assertIn('--show-managed-fields=true', cli.call_args.args)

    def test_completed_defaulted_job_keeps_uid_without_any_job_write(self):
        old = existing_job()
        with patch.object(storage, 'get_job', return_value=old), patch.object(storage, 'kubectl') as cli:
            action = storage.reconcile(render.render('bootstrap', 'demo-node'))
        self.assertEqual(action, 'keep')
        self.assertEqual(cli.call_count, 1)
        payload = cli.call_args.kwargs['data']
        self.assertFalse(any(obj['kind'] == 'Job' for obj in payload['items']))
        self.assertTrue(any(obj['kind'] == 'PersistentVolume' for obj in payload['items']))
        self.assertEqual(old['metadata']['uid'], 'old-uid')

    def test_locking_same_tag_does_not_replace_completed_job(self):
        desired = desired_job()
        desired['spec']['template']['spec']['containers'][0]['image'] += '@sha256:' + 'a'*64
        self.assertEqual(storage.plan(existing_job(), desired), 'keep')

    def test_changed_command_replaces_completed_owned_job(self):
        desired = desired_job()
        desired['spec']['template']['spec']['containers'][0]['args'][-1] += ' && true'
        self.assertEqual(storage.plan(existing_job(), desired), 'replace')

    def test_api_canonical_quantities_preserve_completed_job(self):
        old = existing_job()
        resources = old['spec']['template']['spec']['containers'][0]['resources']
        resources['limits']['memory'] = '67108864'
        resources['requests']['cpu'] = '0.05'
        resources['requests']['memory'] = '16384Ki'
        self.assertEqual(storage.plan(old, desired_job()), 'keep')

    def test_omitted_false_volume_mount_keeps_completed_job(self):
        old = existing_job()
        self.assertNotIn('readOnly', old['spec']['template']['spec']['containers'][0]['volumeMounts'][0])
        self.assertEqual(storage.plan(old, desired_job()), 'keep')

    def test_true_read_only_mount_remains_incompatible(self):
        old = existing_job(None)
        old['spec']['template']['spec']['containers'][0]['volumeMounts'][0]['readOnly'] = True
        with self.assertRaisesRegex(storage.StorageError, 'incompatible'):
            storage.plan(old, desired_job())
        old['status'] = {'conditions':[{'type':'Complete', 'status':'True'}]}
        self.assertEqual(storage.plan(old, desired_job()), 'replace')

    def test_different_actual_resource_quantity_remains_incompatible(self):
        for resource, value in (('cpu', '2'), ('memory', '65Mi')):
            with self.subTest(resource=resource):
                old = existing_job(None)
                old['spec']['template']['spec']['containers'][0]['resources']['limits'][resource] = value
                with self.assertRaisesRegex(storage.StorageError, 'incompatible'):
                    storage.plan(old, desired_job())
                old['status'] = {'conditions':[{'type':'Complete', 'status':'True'}]}
                self.assertEqual(storage.plan(old, desired_job()), 'replace')

    def test_quantity_comparison_is_exact_and_invalid_values_are_not_coerced(self):
        self.assertEqual(storage.quantity_value('1000m'), storage.quantity_value('1'))
        self.assertEqual(storage.quantity_value('1e3'), storage.quantity_value('1k'))
        self.assertEqual(storage.quantity_value('1.5Gi'), storage.quantity_value('1536Mi'))
        self.assertNotEqual(storage.quantity_value('1m'), storage.quantity_value('1.001m'))
        for invalid in ('1CPU', '1e99999999', 'NaN', '1K'):
            self.assertEqual(storage.quantity_value(invalid), invalid)

    def test_failed_owned_job_is_recreated_with_delete_preconditions(self):
        old = existing_job('Failed')
        with patch.object(storage, 'get_job', side_effect=[old, None]), patch.object(storage, 'kubectl', return_value='') as cli:
            self.assertEqual(storage.reconcile(render.render('bootstrap', 'demo-node')), 'replace')
        calls = cli.call_args_list
        self.assertEqual(calls[1].args[0], 'delete')
        self.assertEqual(calls[1].kwargs['data']['preconditions'], {'uid':'old-uid', 'resourceVersion':'27'})
        self.assertEqual(calls[1].kwargs['data']['propagationPolicy'], 'Foreground')
        self.assertEqual(calls[2].args[0], 'create')
        self.assertEqual(calls[2].kwargs['data']['metadata']['annotations'][storage.OWNER], 'true')
        self.assertEqual(calls[3].args[2], 'wait')
        self.assertFalse(any(call.args[0] == 'delete' and 'persistent' in str(call.args) for call in calls))

    def test_active_compatible_job_is_waited_for_without_replacement(self):
        with patch.object(storage, 'get_job', return_value=existing_job(None)), patch.object(storage, 'kubectl') as cli:
            self.assertEqual(storage.reconcile(render.render('bootstrap', 'demo-node')), 'wait')
        self.assertEqual(cli.call_count, 2)
        self.assertEqual(cli.call_args.args[2], 'wait')

    def test_active_image_change_is_refused_before_any_mutation(self):
        old = existing_job(None)
        old['spec']['template']['spec']['containers'][0]['image'] = 'unrelated:image'
        with patch.object(storage, 'get_job', return_value=old), patch.object(storage, 'kubectl') as cli:
            with self.assertRaisesRegex(storage.StorageError, 'active.*incompatible'):
                storage.reconcile(render.render('bootstrap', 'demo-node'))
        cli.assert_not_called()

    def test_matching_labels_without_ownership_evidence_are_foreign(self):
        old = existing_job('Failed')
        old['metadata'].pop('managedFields')
        with patch.object(storage, 'get_job', return_value=old), patch.object(storage, 'kubectl') as cli:
            with self.assertRaisesRegex(storage.StorageError, 'foreign'):
                storage.reconcile(render.render('bootstrap', 'demo-node'))
        cli.assert_not_called()

    def test_missing_uid_never_deletes(self):
        old = existing_job('Failed')
        old['metadata'].pop('uid')
        with patch.object(storage, 'kubectl') as cli:
            with self.assertRaises(storage.StorageError): storage.delete_terminal(old)
        cli.assert_not_called()

    def test_new_job_uses_create_and_then_wait(self):
        with patch.object(storage, 'get_job', return_value=None), patch.object(storage, 'kubectl') as cli:
            self.assertEqual(storage.reconcile(render.render('bootstrap', 'demo-node')), 'create')
        self.assertEqual([call.args[0] for call in cli.call_args_list], ['apply', 'create', '-n'])

    def test_deletion_race_never_overwrites_new_job(self):
        replacement = existing_job('Failed')
        replacement['metadata']['uid'] = 'another-uid'
        with patch.object(storage, 'get_job', return_value=replacement), patch.object(storage, 'kubectl') as cli:
            with self.assertRaisesRegex(storage.StorageError, 'Another storage Job'):
                storage.delete_terminal(existing_job('Failed'))
        self.assertEqual(cli.call_count, 1)

    def test_reject_terminating_job(self):
        old = existing_job()
        old['metadata']['deletionTimestamp'] = '2026-10-03T12:00:00Z'
        with self.assertRaisesRegex(storage.StorageError, 'terminating'): storage.plan(old, desired_job())

    def test_unknown_template_changes_are_not_hidden_by_defaults(self):
        old = existing_job(None)
        old['spec']['template']['spec']['hostNetwork'] = True
        with self.assertRaisesRegex(storage.StorageError, 'incompatible'): storage.plan(old, desired_job())


class AcceptanceProvenance(unittest.TestCase):
    def setUp(self):
        self.host = {'kernel':'6.8.0-ubuntu', 'architecture':'x86_64', 'virtualization':'kvm', 'container':False,
                     'wsl_environment':False, 'owned_cluster_marker':True, 'local_apiserver_manifest':True,
                     'configured_node_ip':'192.168.56.10', 'ipv4':['127.0.0.1', '192.168.56.10']}
        self.nodes = {'items':[{'metadata':{'name':'demo-node', 'labels':{'node-role.kubernetes.io/control-plane':''}},
                     'spec':{}, 'status':{'nodeInfo':{'kubeletVersion':'v1.35.9', 'osImage':'Ubuntu 24.04.3 LTS'},
                     'addresses':[{'type':'InternalIP', 'address':'192.168.56.10'}]}}]}
        self.config = {'data':{'ClusterConfiguration':'apiVersion: kubeadm.k8s.io/v1beta4\nkind: ClusterConfiguration\nkubernetesVersion: v1.35.9\n'}}

    def validate(self):
        return acceptance.validate_kubeadm_provenance(self.nodes, self.config, self.host, 'v1.35.9')

    def test_local_ubuntu_kubeadm_vm_is_accepted(self):
        self.assertTrue(self.validate()['kubeadm_configuration_matches'])

    def test_wsl_kernel_is_rejected(self):
        self.host['kernel'] = '6.6.87.2-microsoft-standard-WSL2'
        with self.assertRaisesRegex(acceptance.CheckError, 'WSL'): self.validate()

    def test_container_host_is_rejected(self):
        self.host['container'] = True
        with self.assertRaisesRegex(acceptance.CheckError, 'container'): self.validate()

    def test_kind_cannot_pass_with_a_forged_profile_file(self):
        self.nodes['items'][0]['spec']['providerID'] = 'kind://docker/signal/signal-control-plane'
        with self.assertRaisesRegex(acceptance.CheckError, 'kind'): self.validate()

    def test_remote_node_is_rejected(self):
        self.host['ipv4'] = ['127.0.0.1']
        with self.assertRaisesRegex(acceptance.CheckError, 'local host address'): self.validate()

    def test_wrong_node_os_is_rejected(self):
        self.nodes['items'][0]['status']['nodeInfo']['osImage'] = 'Debian GNU/Linux 12'
        with self.assertRaisesRegex(acceptance.CheckError, 'Ubuntu 24.04'): self.validate()

    def test_missing_kubeadm_configuration_is_rejected(self):
        self.config['data'] = {}
        with self.assertRaisesRegex(acceptance.CheckError, 'ClusterConfiguration'): self.validate()

    def test_preflight_precedes_package_changes(self):
        script = (ROOT/'scripts/bootstrap-ubuntu.sh').read_text()
        early = script.index('kubectl --kubeconfig=/etc/kubernetes/admin.conf')
        self.assertLess(early, script.index('apt-get update'))
        self.assertLess(script.index('Existing cluster version $actual differs'), script.index('apt-get install'))

    def test_explicit_node_ip_is_also_used_by_kubelet(self):
        script = (ROOT/'scripts/bootstrap-ubuntu.sh').read_text()
        self.assertIn('kubeletExtraArgs:\n    - name: node-ip\n      value: "$NODE_IP"', script)

    def test_locked_kubernetes_packages_allow_newer_vm_tools_to_downgrade(self):
        script = (ROOT/'scripts/bootstrap-ubuntu.sh').read_text()
        locked_install = script.index('apt-get install -y --allow-change-held-packages --allow-downgrades')
        self.assertLess(script.index('Existing cluster version $actual differs'), locked_install)

    def test_manifest_existence_check_uses_noninteractive_read_only_sudo(self):
        responses = [SimpleNamespace(returncode=0, stdout='kvm\n'),
                     SimpleNamespace(returncode=1, stdout='none\n'),
                     SimpleNamespace(returncode=0, stdout='')]
        with patch.object(acceptance.subprocess, 'run', side_effect=responses) as process, \
             patch.object(acceptance.os, 'geteuid', return_value=1001, create=True), \
             patch.object(acceptance, 'run', return_value='[]'), \
             patch.object(acceptance.Path, 'is_file', return_value=True), \
             patch.object(acceptance.Path, 'read_text', return_value='192.168.56.10\n'):
            self.assertTrue(acceptance.host_provenance()['local_apiserver_manifest'])
        self.assertEqual(process.call_args.args[0], ['sudo', '-n', 'test', '-f', '/etc/kubernetes/manifests/kube-apiserver.yaml'])


class BootstrapContainerdConfiguration(unittest.TestCase):
    def configure(self, major: str, fixture: str) -> dict:
        bootstrap = (ROOT/'scripts/bootstrap-ubuntu.sh').read_text()
        source = bootstrap.split('python3 - "$runtime_major" "$pause_image" .state/containerd.default.toml .state/containerd.toml <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path/'default.toml').write_text(fixture)
            args = ['config-test', major, 'registry.k8s.io/pause:3.10.2', str(path/'default.toml'), str(path/'result.toml')]
            with patch.object(sys, 'argv', args):
                exec(compile(source, str(ROOT/'scripts/bootstrap-ubuntu.sh'), 'exec'), {})
            return tomllib.loads((path/'result.toml').read_text())

    def test_containerd1_uses_original_cri_sandbox_and_systemd_options(self):
        result = self.configure('1', '''version = 2
disabled_plugins = []
[plugins."io.containerd.grpc.v1.cri"]
sandbox_image = "old-pause"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
runtime_type = "io.containerd.runc.v2"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
SystemdCgroup = false
''')
        cri = result['plugins']['io.containerd.grpc.v1.cri']
        self.assertEqual(cri['sandbox_image'], 'registry.k8s.io/pause:3.10.2')
        self.assertTrue(cri['containerd']['runtimes']['runc']['options']['SystemdCgroup'])

    def test_containerd2_uses_split_cri_plugins_and_preserves_other_defaults(self):
        result = self.configure('2', '''version = 3
disabled_plugins = []
[plugins.'io.containerd.cri.v1.images']
snapshotter = 'overlayfs'
[plugins.'io.containerd.cri.v1.images'.pinned_images]
sandbox = 'old-pause'
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc]
runtime_type = 'io.containerd.runc.v2'
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]
''')
        images = result['plugins']['io.containerd.cri.v1.images']
        runtime = result['plugins']['io.containerd.cri.v1.runtime']
        self.assertEqual(images['pinned_images']['sandbox'], 'registry.k8s.io/pause:3.10.2')
        self.assertEqual(images['snapshotter'], 'overlayfs')
        self.assertTrue(runtime['containerd']['runtimes']['runc']['options']['SystemdCgroup'])

    def test_unknown_containerd_config_version_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unexpected.*version'):
            self.configure('2', 'version = 5\n')

    def test_containerd23_config4_preserves_server_plugin_settings(self):
        result = self.configure('2', '''version = 4
disabled_plugins = []
[plugins.'io.containerd.server.v1.grpc']
address = '/run/containerd/containerd.sock'
[plugins.'io.containerd.cri.v1.images'.pinned_images]
sandbox = 'old-pause'
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc]
runtime_type = 'io.containerd.runc.v2'
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]
SystemdCgroup = false
''')
        self.assertEqual(result['version'], 4)
        self.assertEqual(result['plugins']['io.containerd.server.v1.grpc']['address'], '/run/containerd/containerd.sock')
        self.assertEqual(result['plugins']['io.containerd.cri.v1.images']['pinned_images']['sandbox'], 'registry.k8s.io/pause:3.10.2')
        self.assertTrue(result['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes']['runc']['options']['SystemdCgroup'])


if __name__ == '__main__':
    unittest.main()
