#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
need kubectl; need helm; need python3; need openssl
lock
[[ -f .state/profile ]] || die 'Run the kubeadm or kind bootstrap first. Existing arbitrary clusters are not a supported default.'
minor=$(kubectl get --raw=/version | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["major"]+"."+d["minor"].rstrip("+"))')
[[ "$minor" == "$KUBERNETES_MINOR" ]] || die "Expected Kubernetes $KUBERNETES_MINOR.x, found $minor"
kubectl -n kube-system get daemonset/cilium >/dev/null
node=$(kubectl get nodes -o json | python3 -c 'import json,sys; d=json.load(sys.stdin)["items"]; assert len(d)==1,"Default profile requires exactly one node"; print(d[0]["metadata"]["labels"]["kubernetes.io/hostname"])')
python3 scripts/render.py --stage bootstrap --node "$node" > .state/bootstrap.json
python3 scripts/storage_init.py .state/bootstrap.json
python3 scripts/create_secrets.py
chart=oci://docker.io/envoyproxy/gateway-helm
if [[ -f vendor/gateway-helm-${ENVOY_GATEWAY_VERSION}.tgz ]]; then
  (cd vendor && sha256sum --check SHA256SUMS)
  chart="$ROOT/vendor/gateway-helm-${ENVOY_GATEWAY_VERSION}.tgz"
fi
helm upgrade --install eg "$chart" --version "$ENVOY_GATEWAY_VERSION" \
  -n envoy-gateway-system --create-namespace -f infra/envoy-values.yaml --atomic --wait --timeout 10m
for crd in gateways.gateway.networking.k8s.io httproutes.gateway.networking.k8s.io envoyproxies.gateway.envoyproxy.io; do
  kubectl wait --for=condition=Established "crd/$crd" --timeout=90s
done
python3 scripts/render.py > .state/application.json
kubectl apply --server-side --dry-run=server --field-manager=signal -f .state/application.json >/dev/null
kubectl apply --server-side --field-manager=signal -f .state/application.json
for name in web-stable web-canary; do
  kubectl -n signal rollout status "deployment/$name" --timeout=5m
done
for name in loki prometheus grafana blackbox alertmanager; do
  kubectl -n signal-observe rollout status "deployment/$name" --timeout=5m
done
kubectl -n signal-logging rollout status daemonset/fluentd --timeout=5m
kubectl -n signal-system rollout status daemonset/node-exporter --timeout=5m
kubectl -n signal wait --for=condition=Programmed gateway/signal --timeout=5m
log 'Deployment completed. Now run make verify; deployment success alone is not acceptance.'
