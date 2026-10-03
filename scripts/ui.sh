#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
need kubectl
pids=()
cleanup() { for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM
for mapping in 'grafana 3000' 'prometheus 9090' 'loki 3100' 'alertmanager 9093'; do
  read -r service port <<< "$mapping"
  kubectl -n signal-observe port-forward --address=127.0.0.1 "service/$service" "$port:$port" &
  pids+=("$!")
done
log 'Local-only UI: Grafana http://127.0.0.1:3000, Prometheus :9090, Loki :3100, Alertmanager :9093.'
log 'Grafana login: admin. Read the generated password explicitly with the README command. Ctrl+C stops forwarding.'
# Exit and close all forwards if any of them fails (for example, an occupied port).
wait -n "${pids[@]}"
