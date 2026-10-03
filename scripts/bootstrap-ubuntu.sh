#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
[[ "${1:-}" == '--dedicated-node' ]] || die 'Use --dedicated-node only on a disposable, dedicated Ubuntu 24.04 VM. This changes swap, sysctl and containerd.'
[[ $EUID -eq 0 ]] || die 'Run with sudo.'
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || die 'The kubeadm bootstrap requires Ubuntu 24.04.'
[[ "$(uname -m)" == x86_64 ]] || die 'Only amd64 has been selected for this bundle.'
[[ $(nproc) -ge 2 ]] || die 'At least 2 vCPU required; 4 recommended.'
[[ $(awk '/MemTotal/ {print $2}' /proc/meminfo) -ge 6000000 ]] || die 'At least 6 GiB RAM required; 8–12 GiB recommended.'
if [[ -e /etc/kubernetes/admin.conf && ! -e /etc/signal-owned-cluster ]]; then
  die 'An unrelated Kubernetes cluster already exists. Refusing to modify it.'
fi
lock
if [[ -e /etc/kubernetes/admin.conf ]]; then
  # Check the existing control plane before apt can change kubelet or tooling.
  # Use its own admin configuration, not a possibly unrelated exported context.
  need kubectl; need python3
  kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=15s get --raw=/readyz \
    | grep -q '^ok' || die 'Existing control plane is not ready. No package or runtime changes were made.'
  actual=$(kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=15s get --raw=/version \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["gitVersion"])')
  [[ "$actual" == "v$KUBERNETES_VERSION" ]] || die "Existing cluster version $actual differs from locked version. Automatic upgrades are not supported; no package or runtime changes were made."
fi
log 'Installing signed Ubuntu OS packages and fixed-version Kubernetes packages.'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg python3 openssl make git conntrack socat ethtool iproute2 iptables util-linux
# Docker's containerd.io bundles both containerd and runc and conflicts with the
# Ubuntu packages. Keep it on dedicated hosts such as GitHub's Ubuntu VM image;
# never remove Docker or its container data to satisfy the runtime dependency.
runtime_package=containerd
if dpkg-query -W -f='${db:Status-Status}' containerd.io 2>/dev/null | grep -qx installed; then
  runtime_package=containerd.io
  need containerd
  log 'Using the installed containerd.io package; Docker packages and data are preserved.'
else
  apt-get install -y containerd
fi
install -d -m 755 /etc/apt/keyrings
curl --fail --location --retry 3 "https://pkgs.k8s.io/core:/stable:/v${KUBERNETES_MINOR}/deb/Release.key" \
  | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
printf 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v%s/deb/ /\n' \
  "$KUBERNETES_MINOR" > /etc/apt/sources.list.d/kubernetes.list
apt-get update
# Dedicated VM images can already ship a newer kubectl. Installing the locked
# trio must allow that explicit downgrade after the existing-cluster preflight.
apt-get install -y --allow-change-held-packages --allow-downgrades \
  "kubelet=${KUBERNETES_VERSION}-1.1" "kubeadm=${KUBERNETES_VERSION}-1.1" "kubectl=${KUBERNETES_VERSION}-1.1"
apt-mark hold kubelet kubeadm kubectl
bash scripts/install-tools.sh
log 'Preparing kernel networking and the CRI runtime.'
modprobe overlay
modprobe br_netfilter
printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/signal.conf
cat > /etc/sysctl.d/99-signal.conf <<'CONF'
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
CONF
sysctl --system >/dev/null
swapoff -a
[[ -e /etc/fstab.signal-before ]] || cp -a /etc/fstab /etc/fstab.signal-before
python3 - <<'PY'
from pathlib import Path
p = Path('/etc/fstab')
lines = []
for line in p.read_text().splitlines(keepends=True):
    fields = line.split()
    if fields and not line.lstrip().startswith('#') and len(fields) > 2 and fields[2] == 'swap':
        line = '# SIGNAL disabled swap: ' + line
    lines.append(line)
p.write_text(''.join(lines))
PY
mkdir -p /etc/containerd
runtime_version=$(containerd --version)
[[ "$runtime_version" =~ (^|[[:space:]])v?([12])\.[0-9] ]] || die "Unsupported containerd version: $runtime_version. Only majors 1 and 2 have an explicit configuration path."
runtime_major="${BASH_REMATCH[2]}"
log "Configuring $runtime_version with its native defaults."
containerd config default > .state/containerd.default.toml
pause_image=$(kubeadm config images list --kubernetes-version "v$KUBERNETES_VERSION" | grep '/pause:')
# Start from this binary's defaults, modify the major-specific CRI tables, and
# validate the result before replacing the host configuration. Ubuntu 24.04
# provides Python 3.12, including the standard-library TOML reader.
python3 - "$runtime_major" "$pause_image" .state/containerd.default.toml .state/containerd.toml <<'PY'
import json, re, sys, tomllib
from pathlib import Path
major, pause, source, target = sys.argv[1:]
raw = Path(source).read_text()
original = tomllib.loads(raw)
supported_versions = (2,) if major == '1' else (3, 4)
if original.get('version') not in supported_versions:
    raise RuntimeError(f'Unexpected containerd default config version {original.get("version")}; expected {supported_versions}; refusing to rewrite host config')
runtime_plugin = 'io.containerd.grpc.v1.cri' if major == '1' else 'io.containerd.cri.v1.runtime'
original['plugins'][runtime_plugin]['containerd']['runtimes']['runc']
if original.get('disabled_plugins'):
    raise RuntimeError('Unexpected disabled plugins in containerd defaults')

def set_value(section, key, value):
    global raw
    lines = raw.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = re.fullmatch(r'\s*\[([^\]]+)\]\s*', line)
        if match and re.sub(r'[\s\"\']', '', match[1]) == re.sub(r'[\s\"\']', '', section):
            start = index + 1
            break
    rendered = f'  {key} = {value}'
    if start is None:
        lines.extend(['', f'[{section}]', rendered])
    else:
        end = next((index for index in range(start, len(lines)) if lines[index].lstrip().startswith('[')), len(lines))
        assignments = [index for index in range(start, end) if re.match(r'\s*' + re.escape(key) + r'\s*=', lines[index])]
        if len(assignments) > 1:
            raise RuntimeError(f'Duplicate containerd setting: {key}')
        if assignments:
            lines[assignments[0]] = rendered
        else:
            lines.insert(start, rendered)
    raw = '\n'.join(lines) + '\n'

runtime_section = f'plugins."{runtime_plugin}".containerd.runtimes.runc.options'
set_value(runtime_section, 'SystemdCgroup', 'true')
if major == '1':
    set_value('plugins."io.containerd.grpc.v1.cri"', 'sandbox_image', json.dumps(pause))
else:
    original['plugins']['io.containerd.cri.v1.images']
    set_value('plugins."io.containerd.cri.v1.images".pinned_images', 'sandbox', json.dumps(pause))
updated = tomllib.loads(raw)
if updated['plugins'][runtime_plugin]['containerd']['runtimes']['runc']['options']['SystemdCgroup'] is not True:
    raise RuntimeError('containerd systemd cgroup setting was not applied')
sandbox = (updated['plugins'][runtime_plugin]['sandbox_image'] if major == '1' else
           updated['plugins']['io.containerd.cri.v1.images']['pinned_images']['sandbox'])
if sandbox != pause:
    raise RuntimeError('containerd sandbox image does not match kubeadm')
Path(target).write_text(raw)
PY
if ! cmp -s .state/containerd.toml /etc/containerd/config.toml; then
  if [[ -f /etc/containerd/config.toml && ! -e /etc/containerd/config.toml.signal-before ]]; then
    cp -a /etc/containerd/config.toml /etc/containerd/config.toml.signal-before
  fi
  install -m 644 .state/containerd.toml /etc/containerd/config.toml
  systemctl restart containerd
fi
systemctl enable --now containerd kubelet
NODE_IP="${NODE_IP:-$(ip -4 route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')}"
export NODE_IP KUBERNETES_VERSION
python3 - <<'PY'
import ipaddress, os
ipaddress.IPv4Address(os.environ['NODE_IP'])
PY
if [[ ! -e /etc/kubernetes/admin.conf ]]; then
  touch /etc/signal-owned-cluster
  cat > .state/kubeadm.yaml <<CONF
apiVersion: kubeadm.k8s.io/v1beta4
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: "$NODE_IP"
nodeRegistration:
  criSocket: unix:///run/containerd/containerd.sock
  kubeletExtraArgs:
    - name: node-ip
      value: "$NODE_IP"
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
kubernetesVersion: "v$KUBERNETES_VERSION"
networking:
  podSubnet: 10.244.0.0/16
  serviceSubnet: 10.96.0.0/12
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd
containerLogMaxSize: 10Mi
containerLogMaxFiles: 5
CONF
  kubeadm init --config .state/kubeadm.yaml --skip-token-print
fi
install -m 600 /etc/kubernetes/admin.conf "$KUBECONFIG"
kubectl get --raw=/readyz | grep -q '^ok' || die 'Existing control plane is not ready. No destructive reset is performed.'
actual=$(kubectl get --raw=/version | python3 -c 'import sys,json; print(json.load(sys.stdin)["gitVersion"])')
[[ "$actual" == "v$KUBERNETES_VERSION" ]] || die "Existing cluster version $actual differs from locked version. Automatic upgrades are not supported."
# This is explicitly a single-node demo, not an HA control plane.
kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>/dev/null || true
bash scripts/install-cni.sh
printf 'kubeadm\n' > .state/profile
printf '%s\n' "$NODE_IP" > .state/node-ip
dpkg-query -W "$runtime_package" kubeadm kubelet kubectl > .state/os-package-versions.txt
printf '%s\n' "$runtime_version" >> .state/os-package-versions.txt
runc --version >> .state/os-package-versions.txt
if [[ -n "${SUDO_UID:-}" ]]; then
  chown -R "${SUDO_UID}:${SUDO_GID}" .state .tools
fi
log 'Cluster prepared. Continue as the normal user: make deploy && make verify'
