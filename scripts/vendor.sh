#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
need helm; need sha256sum
mkdir -p vendor
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
helm pull cilium --repo https://helm.cilium.io --version "$CILIUM_VERSION" --destination "$tmp"
helm pull oci://docker.io/envoyproxy/gateway-helm --version "$ENVOY_GATEWAY_VERSION" --destination "$tmp"
install -m 644 "$tmp/cilium-${CILIUM_VERSION}.tgz" "vendor/cilium-${CILIUM_VERSION}.tgz"
files=("$tmp"/gateway-helm-*.tgz)
[[ ${#files[@]} -eq 1 && -f ${files[0]} ]] || die 'Expected exactly one Envoy Gateway chart.'
install -m 644 "${files[0]}" "vendor/gateway-helm-${ENVOY_GATEWAY_VERSION}.tgz"
(cd vendor && sha256sum "cilium-${CILIUM_VERSION}.tgz" "gateway-helm-${ENVOY_GATEWAY_VERSION}.tgz" > SHA256SUMS)
log 'Charts and checksums saved. Add them to Git explicitly; this does not vendor container images or OS packages.'
