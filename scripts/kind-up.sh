#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
need docker; need python3; need openssl
lock
bash scripts/install-tools.sh --kind
docker info >/dev/null || die 'Docker daemon is unavailable.'
if kind get clusters | grep -qx signal; then
  kind export kubeconfig --name signal --kubeconfig "$KUBECONFIG"
else
  kind create cluster --name signal --image "$KIND_NODE_IMAGE" --config infra/kind.yaml --kubeconfig "$KUBECONFIG" --wait 0s
fi
chmod 600 "$KUBECONFIG"
bash scripts/install-cni.sh
printf 'kind\n' > .state/profile
printf '127.0.0.1\n' > .state/node-ip
log 'kind profile ready. The node image uses Kubernetes 1.35.8, not the kubeadm patch 1.35.9.'
