#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
need docker; need python3
mkdir -p .state/checks
python3 - <<'PY'
import sys,json
sys.path.insert(0,'scripts');import render
from pathlib import Path
p=Path('.state/checks');(p/'nginx.conf').write_text(render.nginx_config('stable'))
(p/'prometheus.yml').write_text(json.dumps(render.prom_config()))
(p/'alerts.yaml').write_text(render.read('alerts.yaml'))
PY
image() { python3 -c "import sys;sys.path.insert(0,'scripts');import render;print(render.images()['$1'])"; }
# This checks the exact selected image versions; it is separate from local nginx tests.
docker run --rm --entrypoint nginx -v "$ROOT/.state/checks/nginx.conf:/etc/signal/nginx.conf:ro" "$(image nginx)" -t -c /etc/signal/nginx.conf
docker run --rm --entrypoint /bin/promtool -v "$ROOT/.state/checks:/etc/prometheus:ro" "$(image prometheus)" check config --syntax-only /etc/prometheus/prometheus.yml
docker run --rm --entrypoint /bin/promtool -v "$ROOT/config/alerts.yaml:/rules.yaml:ro" "$(image prometheus)" check rules /rules.yaml
# Use the evaluator from the exact locked image for behavioral alert scenarios.
promtool_container=$(docker create --entrypoint /bin/promtool "$(image prometheus)")
trap 'docker rm "$promtool_container" >/dev/null 2>&1 || true' EXIT
mkdir -p .tools
docker cp "$promtool_container:/bin/promtool" .tools/promtool
docker rm "$promtool_container" >/dev/null
trap - EXIT
chmod +x .tools/promtool
python3 -m unittest discover -s tests -p test_alerts.py -v
docker run --rm --entrypoint /usr/bin/loki -v "$ROOT/config/loki.yaml:/etc/loki/loki.yaml:ro" "$(image loki)" -config.file=/etc/loki/loki.yaml -verify-config=true
docker run --rm --user 0 --entrypoint fluentd --tmpfs /buffers --tmpfs /tmp \
  -v "$ROOT/config/fluent.conf:/fluentd/etc/fluent.conf:ro" "$(image fluentd)" --dry-run -c /fluentd/etc/fluent.conf -p /fluentd/plugins
docker run --rm --entrypoint /bin/amtool -v "$ROOT/config/alertmanager.yaml:/alertmanager.yaml:ro" "$(image alertmanager)" check-config /alertmanager.yaml
log 'Selected container configuration checks completed.'
