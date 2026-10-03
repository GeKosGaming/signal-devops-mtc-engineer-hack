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
# A dry run does not start Ruby's supervisor or the monitor HTTP listener.
# /tmp deliberately lacks the sticky bit, as a kubelet-created emptyDir can.
fluentd_container=$(docker run --detach --read-only --cap-drop=ALL \
  --security-opt no-new-privileges --user 0 --entrypoint fluentd \
  --tmpfs /buffers:rw,mode=0700 --tmpfs /tmp:rw,mode=0777 -e TMPDIR=/buffers \
  --publish 127.0.0.1::24220 \
  -v "$ROOT/config/fluent.conf:/fluentd/etc/fluent.conf:ro" \
  "$(image fluentd)" -c /fluentd/etc/fluent.conf -p /fluentd/plugins)
trap 'docker rm --force "$fluentd_container" >/dev/null 2>&1 || true' EXIT
fluentd_endpoint=$(docker port "$fluentd_container" 24220/tcp)
if ! python3 - "$fluentd_endpoint" <<'PY'
import json, sys, time, urllib.request
endpoint = sys.argv[1]
if not endpoint.startswith('127.0.0.1:') or not endpoint.rsplit(':', 1)[1].isdigit():
    raise SystemExit('Fluentd monitor was not published exclusively on loopback')
deadline = time.monotonic() + 30
last_error = None
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
while time.monotonic() < deadline:
    try:
        with opener.open('http://' + endpoint + '/api/plugins.json', timeout=2) as r:
            data = json.load(r)
        ids = {p.get('plugin_id') for p in data.get('plugins', [])}
        if not {'app_container_logs', 'loki_output'} <= ids:
            raise RuntimeError('Expected Fluentd input and Loki output are not running')
        print('PASS Fluentd starts with the deployed filesystem and capability constraints')
        break
    except (OSError, ValueError, RuntimeError) as exc:
        last_error = exc
        time.sleep(0.5)
else:
    raise SystemExit(f'Fluentd startup smoke failed: {last_error}')
PY
then
  docker logs --tail=200 "$fluentd_container" >&2 || true
  die 'Fluentd did not start with the deployed container constraints.'
fi
docker rm --force "$fluentd_container" >/dev/null
trap - EXIT
docker run --rm --entrypoint /bin/amtool -v "$ROOT/config/alertmanager.yaml:/alertmanager.yaml:ro" "$(image alertmanager)" check-config /alertmanager.yaml
log 'Selected container configuration checks completed.'
