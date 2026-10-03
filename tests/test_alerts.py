"""Execute real PromQL alert scenarios when promtool is installed; never fake PASS."""
from __future__ import annotations
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = shutil.which('promtool') or (str(ROOT/'.tools/promtool') if (ROOT/'.tools/promtool').is_file() else None)


@unittest.skipUnless(TOOL, 'promtool not installed; PromQL behavioral tests require the real evaluator')
class CapacityAlerts(unittest.TestCase):
    def evaluate(self, series: str, checks: str):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); shutil.copyfile(ROOT/'config/alerts.yaml', folder/'alerts.yaml')
            (folder/'cases.yaml').write_text('rule_files: [alerts.yaml]\nevaluation_interval: 15s\ntests:\n'
                '  - interval: 1m\n    input_series:\n'+series+'    alert_rule_test:\n'+checks, encoding='utf-8')
            result = subprocess.run([TOOL, 'test', 'rules', 'cases.yaml'], cwd=folder, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
    def expected(self, name, summary, labels):
        return (f'      - eval_time: 3m\n        alertname: {name}\n        exp_alerts:\n'
                f'          - exp_labels: {{{labels}}}\n            exp_annotations:\n'
                f'              summary: {summary}\n              runbook: docs/RUNBOOK.md#metrics\n')
    def test_completely_missing_discovered_jobs_fire(self):
        checks = ''.join([
            self.expected('SignalNginxStableCapacityLow', 'Fewer than two healthy stable Nginx targets for two minutes', 'severity: warning, job: nginx, release: stable'),
            self.expected('SignalNginxCanaryMissing', 'No healthy canary Nginx target for two minutes', 'severity: warning, job: nginx, release: canary'),
            self.expected('SignalEnvoyCapacityLow', 'Fewer than two healthy Envoy targets for two minutes', 'severity: warning, job: envoy'),
            self.expected('SignalNodeMetricsMissing', 'No healthy node-exporter target for two minutes', 'severity: warning, job: node')])
        self.evaluate('      []\n', checks)
    def test_rolling_surge_and_stale_old_pod_do_not_fire_capacity_alert(self):
        series = ('      - series: \'up{job="nginx",release="stable",pod="old"}\'\n        values: \'1x3 stale\'\n'
                  '      - series: \'up{job="nginx",release="stable",pod="other"}\'\n        values: \'1x6\'\n'
                  '      - series: \'up{job="nginx",release="stable",pod="new"}\'\n        values: \'0x2 1x3\'\n')
        self.evaluate(series, '      - eval_time: 5m\n        alertname: SignalNginxStableCapacityLow\n        exp_alerts: []\n'
                      '      - eval_time: 2m\n        alertname: SignalTargetDown\n        exp_alerts: []\n')
    def test_sustained_single_stable_replica_fires(self):
        series = '      - series: \'up{job="nginx",release="stable",pod="only"}\'\n        values: \'1x5\'\n'
        self.evaluate(series, self.expected('SignalNginxStableCapacityLow', 'Fewer than two healthy stable Nginx targets for two minutes', 'severity: warning, job: nginx, release: stable'))
    def test_canary_health_cannot_mask_missing_stable_release(self):
        series = '      - series: \'up{job="nginx",release="canary",pod="canary"}\'\n        values: \'1x5\'\n'
        self.evaluate(series, self.expected('SignalNginxStableCapacityLow', 'Fewer than two healthy stable Nginx targets for two minutes', 'severity: warning, job: nginx, release: stable'))


if __name__ == '__main__': unittest.main()
