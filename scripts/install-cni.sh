#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
need helm; need kubectl
helm repo add cilium https://helm.cilium.io --force-update >/dev/null
helm repo update cilium >/dev/null
chart=cilium/cilium
if [[ -f vendor/cilium-${CILIUM_VERSION}.tgz ]]; then
  (cd vendor && sha256sum --check SHA256SUMS)
  chart="$ROOT/vendor/cilium-${CILIUM_VERSION}.tgz"
fi
helm upgrade --install cilium "$chart" --version "$CILIUM_VERSION" \
  -n kube-system -f infra/cilium-values.yaml --atomic --wait --timeout 10m
kubectl -n kube-system rollout status daemonset/cilium --timeout=5m
kubectl wait --for=condition=Ready nodes --all --timeout=5m
