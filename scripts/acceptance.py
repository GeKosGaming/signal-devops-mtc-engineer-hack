#!/usr/bin/env python3
"""Ubuntu/kubeadm acceptance plus redeploy invariants. No invented PASS results."""
from __future__ import annotations
import argparse, hashlib, json, os, platform, re, subprocess
from runtime import *


def fingerprint() -> dict:
    # No Secret reads. Compare public trust anchor and persistent resource identity.
    pods=k('get','pods','-A','-o','json')['items']
    keep={APP,OBS,'signal-logging','signal-system',EDGE}
    uids=sorted((p['metadata']['namespace'],p['metadata']['name'],p['metadata']['uid']) for p in pods
                if p['metadata']['namespace'] in keep and p['status'].get('phase')=='Running')
    pvc=get('pvc',ns=OBS)['items']
    return {'running_pods':uids,'pvc_uids':sorted((p['metadata']['name'],p['metadata']['uid']) for p in pvc),
            'storage_job_uid':get('job','signal-storage-init',ns='signal-system')['metadata']['uid'],
            'ca_sha256':hashlib.sha256((ROOT/'.state/tls/ca.crt').read_bytes()).hexdigest(),
            'leaf_sha256':hashlib.sha256((ROOT/'.state/tls/server.crt').read_bytes()).hexdigest()}


def host_provenance() -> dict:
    """Read local host evidence without opening admin.conf or private keys."""
    detected = subprocess.run(['systemd-detect-virt'], capture_output=True, text=True, timeout=10)
    require(detected.returncode in (0, 1), 'Cannot determine host virtualization')
    container = subprocess.run(['systemd-detect-virt', '--container'], capture_output=True, text=True, timeout=10)
    require(container.returncode in (0, 1), 'Cannot determine whether the host is a container')
    addresses = json.loads(run(['ip', '-j', '-4', 'address', 'show']))
    ipv4 = sorted({address['local'] for interface in addresses for address in interface.get('addr_info', [])
                   if address.get('family') == 'inet'})
    # kubeadm deliberately creates manifests/ with mode 0700. Ordinary operators
    # can prove existence via a read-only sudo test without reading that manifest.
    manifest = '/etc/kubernetes/manifests/kube-apiserver.yaml'
    manifest_check = subprocess.run(([] if os.geteuid() == 0 else ['sudo', '-n']) + ['test', '-f', manifest],
                                    capture_output=True, text=True, timeout=10)
    require(manifest_check.returncode in (0, 1), 'Cannot check the local control-plane manifest; read-only sudo permission is required')
    return {'kernel':platform.release(), 'architecture':platform.machine(),
            'virtualization':detected.stdout.strip(), 'container':container.returncode == 0,
            'wsl_environment':bool(os.environ.get('WSL_INTEROP') or os.environ.get('WSL_DISTRO_NAME')),
            'ipv4':ipv4, 'owned_cluster_marker':Path('/etc/signal-owned-cluster').is_file(),
            'local_apiserver_manifest':manifest_check.returncode == 0,
            'configured_node_ip':(ROOT/'.state/node-ip').read_text().strip()}


def validate_kubeadm_provenance(nodes: dict, configuration: dict, host: dict, version: str) -> dict:
    require(not host['container'], 'Primary acceptance requires a dedicated Ubuntu host/VM, not a container')
    require(not host['wsl_environment'] and 'microsoft' not in host['kernel'].lower() and 'wsl' not in host['kernel'].lower(),
            'WSL cannot confirm the dedicated Ubuntu kubeadm profile')
    require(host['architecture'] in ('x86_64', 'amd64'), 'Primary acceptance requires an amd64 host')
    require(host['owned_cluster_marker'] and host['local_apiserver_manifest'], 'Local SIGNAL kubeadm control-plane provenance is missing')
    items = nodes['items']
    require(len(items) == 1, 'Primary acceptance requires exactly one local node')
    node = items[0]
    require(not node['spec'].get('providerID', '').startswith('kind://') and
            'kind.x-k8s.io/cluster' not in node['metadata'].get('labels', {}), 'kind nodes cannot confirm kubeadm acceptance')
    require('node-role.kubernetes.io/control-plane' in node['metadata'].get('labels', {}), 'Expected the kubeadm control-plane node')
    info = node['status']['nodeInfo']
    require(info['kubeletVersion'] == version, 'The node kubelet version differs from the accepted control plane')
    require(re.match(r'^Ubuntu 24\.04(?:\.|\s|$)', info['osImage']), 'The Kubernetes node itself must run Ubuntu 24.04')
    internal = {address['address'] for address in node['status']['addresses'] if address['type'] == 'InternalIP'}
    require(host['configured_node_ip'] in internal and host['configured_node_ip'] in host['ipv4'],
            'The configured Kubernetes node IP is not both a local host address and the node InternalIP')
    cluster = configuration.get('data', {}).get('ClusterConfiguration', '')
    require(bool(re.search(r'^kind:\s*ClusterConfiguration\s*$', cluster, re.M)), 'The kubeadm-config ConfigMap is missing ClusterConfiguration')
    require(bool(re.search(r'^kubernetesVersion:\s*[\"\']?' + re.escape(version) + r'[\"\']?\s*$', cluster, re.M)),
            'kubeadm-config does not match the accepted Kubernetes version')
    return {'host':host, 'node':node['metadata']['name'], 'node_os':info['osImage'],
            'kubelet':info['kubeletVersion'], 'kubeadm_configuration_matches':True}


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--ci',action='store_true',help='kind smoke only, cannot confirm Ubuntu kubeadm acceptance')
    a=p.parse_args();r=Report('Redeploy/Ubuntu acceptance' if not a.ci else 'kind CI redeploy smoke','evidence/acceptance' if not a.ci else 'evidence/ci')
    env=r.data['environment']
    try:
        def platform():
            require(env['os'].get('ID')=='ubuntu' and env['os'].get('VERSION_ID')=='24.04','Ubuntu 24.04 host required')
            require(env['profile']==('kind' if a.ci else 'kubeadm'),'Wrong cluster profile')
            version=k('get','--raw=/version')['gitVersion']
            require(version==('v1.35.8' if a.ci else 'v1.35.9'),'Unexpected Kubernetes patch')
            evidence={'os':env['os'],'profile':env['profile'],'kubernetes':version}
            if not a.ci:
                evidence['provenance']=validate_kubeadm_provenance(
                    get('nodes'), get('configmap','kubeadm-config',ns='kube-system'), host_provenance(), version)
            return evidence
        platform_result=r.check('actual platform matches claimed platform',platform)
        require(platform_result is not None,'Acceptance stopped: platform mismatch')
        def deploy(): return {'output':run(['bash','scripts/deploy.sh'],timeout=1800)[-1800:]}
        r.check('first deployment',deploy)
        require(r.data['checks'][-1]['passed'],'First deployment failed')
        before=fingerprint()
        r.check('baseline functional verification',lambda:{'output':run(['python3','scripts/verify.py','--out',str(r.out.relative_to(ROOT)/'before')],timeout=900)[-2000:]})
        r.check('second deployment',deploy)
        after=fingerprint()
        r.data['redeploy_fingerprints']={'before':before,'after':after}
        def invariant():
            require(before==after,'Pods, PVC/Job identities or TLS certificate changed during an unchanged redeploy')
            return {'before':before,'after':after}
        r.check('idempotence: same Pods, PVCs and certificates',invariant)
        r.check('verification after redeploy',lambda:{'output':run(['python3','scripts/verify.py','--out',str(r.out.relative_to(ROOT)/'after')],timeout=900)[-2000:]})
    except Exception as exc:
        r.check('acceptance execution',lambda:require(False,str(exc)))
    r.data['ubuntu_24_04_kubeadm_confirmed']=not a.ci and bool(r.data['checks']) and all(x['passed'] for x in r.data['checks'])
    return 0 if r.save() else 1

if __name__=='__main__': raise SystemExit(main())
