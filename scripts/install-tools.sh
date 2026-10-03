#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || die 'This bundle targets Linux amd64.'
need curl; need sha256sum; need tar
mkdir -p .tools
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
fetch() { curl --fail --location --retry 3 --connect-timeout 15 --max-time 300 "$1" -o "$2"; }
if [[ ! -x .tools/helm ]] || ! .tools/helm version --short | grep -q "^${HELM_VERSION}"; then
  fetch "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" "$work/helm.tgz"
  echo "$HELM_SHA256  $work/helm.tgz" | sha256sum --check -
  tar -xzf "$work/helm.tgz" -C "$work"
  install -m 755 "$work/linux-amd64/helm" .tools/helm
fi
if [[ "${1:-}" == '--kind' ]]; then
  if [[ ! -x .tools/kind ]] || ! .tools/kind version | grep -Fq "$KIND_VERSION"; then
    fetch "https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-linux-amd64" "$work/kind"
    fetch "https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-linux-amd64.sha256sum" "$work/kind.sum"
    digest=$(awk '{print $1}' "$work/kind.sum")
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die 'Invalid kind checksum'
    echo "$digest  $work/kind" | sha256sum --check -
    install -m 755 "$work/kind" .tools/kind
  fi
fi
# Always prefer the selected kubectl over a hosted runner's preinstalled client.
if [[ ! -x .tools/kubectl ]] || ! .tools/kubectl version --client -o json | grep -Fq "v${KUBERNETES_VERSION}"; then
    fetch "https://dl.k8s.io/release/v${KUBERNETES_VERSION}/bin/linux/amd64/kubectl" "$work/kubectl"
    fetch "https://dl.k8s.io/release/v${KUBERNETES_VERSION}/bin/linux/amd64/kubectl.sha256" "$work/kubectl.sha256"
    digest=$(cat "$work/kubectl.sha256")
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die 'Invalid kubectl checksum'
    echo "$digest  $work/kubectl" | sha256sum --check -
    install -m 755 "$work/kubectl" .tools/kubectl
fi
