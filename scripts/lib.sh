#!/usr/bin/env bash
# Sourced by entry points; never enable xtrace when handling kubeconfig/secrets.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
cd "$ROOT"
# shellcheck source=../versions.env
source "$ROOT/versions.env"
export PATH="$ROOT/.tools:$PATH"
export KUBECONFIG="$ROOT/.state/kubeconfig"
mkdir -p "$ROOT/.state"
chmod 700 "$ROOT/.state"
log() { printf '\n[SIGNAL] %s\n' "$*"; }
die() { printf '\n[SIGNAL ERROR] %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "Missing command: $1"; }
lock() {
  if [[ ${SIGNAL_OPERATION_LOCK_FD+x} ]]; then
    # A marker alone never bypasses flock: validate the real inherited FD and
    # workspace inode using the same implementation as Python entry points.
    python3 "$ROOT/scripts/operations.py" --validate-inherited || die 'Cannot use the inherited SIGNAL operation lock.'
  else
    exec 9>"$ROOT/.state/workflow.lock"
    flock -n 9 || die 'Another SIGNAL operation is running.'
    export SIGNAL_OPERATION_LOCK_FD=9
  fi
}
trap 'printf "[SIGNAL] Failed at %s:%s. Read docs/RUNBOOK.md.\n" "${BASH_SOURCE[0]}" "$LINENO" >&2' ERR
