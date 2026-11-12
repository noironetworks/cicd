"""Offline CLI qualification with a stub gh and a genuine loopback HTTPS webhook."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'travis/ci_monitor.py'
spec = importlib.util.spec_from_file_location('ci_monitor_under_test', SCRIPT)
monitor = importlib.util.module_from_spec(spec); spec.loader.exec_module(monitor)
NOW = '2026-11-12T21:00:00Z'
REQUEST = {'repository': 'noironetworks/acc-provision', 'tag': '6.1.1.8', 'source_sha': 'a' * 40,
           'requested_at': '2026-11-12T20:00:00Z', 'deadline': '2026-11-12T20:10:00Z',
           'workflow': 'acc-provision-tag-publish.yml'}


class HTTPSFixture:
    def __init__(self, directory, certificate, key):
        self.events = []; self.status = 202; self.redirect = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                owner.events.append({'body': json.loads(body), 'key': self.headers.get('Idempotency-Key')})
                self.send_response(302 if owner.redirect else owner.status)
                if owner.redirect:
                    self.send_header('Location', 'http://127.0.0.1:1/forbidden')
                self.end_headers()
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(certificate, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'https://127.0.0.1:{self.server.server_port}/owner-notification'
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)


class CIMonitorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tls = tempfile.TemporaryDirectory(prefix='monitor-tls-', dir='/tmp')
        cls.addClassCleanup(cls.tls.cleanup)
        cls.certificate = Path(cls.tls.name) / 'certificate.pem'; cls.key = Path(cls.tls.name) / 'key.pem'
        openssl = shutil.which('openssl')
        if not openssl: raise RuntimeError('openssl is required for offline HTTPS qualification')
        result = subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(cls.key),
            '-out', str(cls.certificate), '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1', '-days', '1'],
            capture_output=True, text=True, timeout=30)
        if result.returncode: raise AssertionError('temporary HTTPS fixture certificate generation failed')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='monitor-test-', dir='/tmp'); self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name); self.bin = self.dir / 'bin'; self.bin.mkdir()
        self.state = self.dir / 'gh-state.json'; self.capture = self.dir / 'gh-calls.jsonl'
        gh = self.bin / 'gh'
        gh.write_text('#!' + sys.executable + '''
import json,os,pathlib,sys
root=pathlib.Path(sys.argv[0]).parent.parent
with (root/'gh-calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'argv':sys.argv[1:],'secret_names':sorted(k for k in os.environ if k in ('GITHUB_TOKEN','GH_TOKEN','TRAVIS_TAGGER','PYPI_PASS','TWINE_PASSWORD','CI_MONITOR_WEBHOOK'))})+'\\n')
state=json.loads((root/'gh-state.json').read_text())
if state.get('return_code'): raise SystemExit(state['return_code'])
if state.get('malformed'): print('invalid json'); raise SystemExit(0)
print(json.dumps(state['runs'] if '/runs' in sys.argv[4] else state['workflow']))
''')
        gh.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'SSL_CERT_FILE': str(self.certificate), 'NO_PROXY': '127.0.0.1,localhost',
                    'no_proxy': '127.0.0.1,localhost', 'GITHUB_TOKEN': 'fixture-read-only',
                    'TRAVIS_TAGGER': 'fixture-value', 'PYPI_PASS': 'fixture-value',
                    'TWINE_PASSWORD': 'fixture-value', 'CI_MONITOR_WEBHOOK': ''}
        self.env.pop('GH_TOKEN', None)
        self.requests = self.dir / 'requests.json'; self.report = self.dir / 'report.json'; self.receipt = self.dir / 'receipt.json'
        self.requests.write_text(json.dumps({'schema_version': 1, 'requests': [REQUEST]}))
        self.set_state([])

    def run_record(self, **updates):
        value = {'id': 7654321, 'run_attempt': 1, 'head_sha': REQUEST['source_sha'],
            'head_repository': {'full_name': REQUEST['repository']}, 'head_branch': REQUEST['tag'],
            'event': 'push', 'path': '.github/workflows/' + REQUEST['workflow'],
            'created_at': '2026-11-12T20:05:00Z', 'status': 'completed', 'conclusion': 'success',
            'html_url': 'https://github.com/noironetworks/acc-provision/actions/runs/7654321'}
        value.update(updates); return value

    def set_state(self, runs, workflow='active', **extra):
        self.state.write_text(json.dumps({'workflow': {'state': workflow},
            'runs': {'total_count': len(runs), 'workflow_runs': runs}, **extra}))

    def cli(self, *args, success=True, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], env=env or self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return json.loads(result.stdout) if success else result

    def check(self, now=NOW):
        result = self.cli('check', '--requests', self.requests, '--output', self.report, '--now', now)
        self.assertEqual(result, json.loads(self.report.read_text()))
        return result

    def webhook(self):
        server = HTTPSFixture(self.dir, self.certificate, self.key); self.addCleanup(server.close)
        return server

    def test_absent_empty_invalid_configuration_remains_unknown_without_queries_or_notifications(self):
        report = self.cli('check', '--output', self.report, '--now', NOW)
        self.assertEqual(report['state'], 'unknown'); self.assertFalse(self.capture.exists())
        for requests in ([], [{**REQUEST, 'deadline': '2026-11-12T19:59:00Z'}], [{**REQUEST, 'requested_at': '2026-11-12T20:00:00'}]):
            self.requests.write_text(json.dumps({'schema_version': 1, 'requests': requests}))
            self.assertEqual(self.check()['state'], 'unknown')
        self.assertFalse(self.capture.exists())
        result = self.cli('notify', '--report', self.report, '--receipt', self.receipt)
        self.assertEqual(result['state'], 'no_alerts'); self.assertFalse(self.receipt.exists())

    def test_explicit_deadline_distinguishes_waiting_and_missed_and_scopes_gh_environment(self):
        self.assertEqual(self.check('2026-11-12T20:09:59Z')['checks'][0]['state'], 'waiting')
        self.assertEqual(self.check()['checks'][0]['state'], 'missed')
        calls = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertTrue(all(c['argv'][:3] == ['api', '--method', 'GET'] for c in calls))
        self.assertTrue(all(c['secret_names'] == ['GITHUB_TOKEN'] for c in calls))
        self.assertIn('branch=6.1.1.8', calls[-1]['argv'])
        self.assertEqual(self.check()['checks'][0]['request']['requested_at'], REQUEST['requested_at'])

    def test_disabled_failed_cancelled_timed_out_and_wrong_source_are_actionable(self):
        self.set_state([], workflow='disabled_manually')
        self.assertEqual(self.check()['checks'][0]['state'], 'disabled')
        for conclusion in ('failure', 'cancelled', 'timed_out', 'skipped'):
            self.set_state([self.run_record(conclusion=conclusion)])
            with self.subTest(conclusion=conclusion): self.assertEqual(self.check()['checks'][0]['state'], 'failed')
        self.set_state([self.run_record(), self.run_record(id=7654322, created_at='2026-11-12T20:06:00Z', head_sha='b' * 40)])
        self.assertEqual(self.check()['checks'][0]['state'], 'wrong-source')

    def test_latest_attempt_pending_cannot_be_hidden_by_success_and_recovery_is_healthy(self):
        self.set_state([self.run_record(), self.run_record(run_attempt=2, status='in_progress', conclusion=None)])
        self.assertEqual(self.check()['checks'][0]['state'], 'pending')
        self.set_state([self.run_record(run_attempt=2)])
        report = self.check(); self.assertEqual(report['state'], 'healthy')
        self.assertEqual(report['checks'][0]['run']['run_attempt'], 2)
        self.set_state([self.run_record(created_at='2026-11-12T20:20:00Z')])
        self.assertTrue(self.check()['checks'][0]['started_late'])

    def test_wrong_workflow_fork_pr_and_old_runs_do_not_satisfy_request(self):
        for changes in ({'event': 'pull_request'}, {'path': '.github/workflows/other.yml'},
                        {'head_repository': {'full_name': 'fork/acc-provision'}},
                        {'created_at': '2026-11-12T19:59:59Z'}, {'head_branch': 'master'}):
            self.set_state([self.run_record(**changes)])
            with self.subTest(changes=changes): self.assertEqual(self.check()['checks'][0]['state'], 'missed')
        self.set_state([], return_code=1)
        self.assertEqual(self.check()['checks'][0]['state'], 'unknown')
        self.set_state([], malformed=True)
        self.assertEqual(self.check()['checks'][0]['state'], 'unknown')
        self.set_state([]); state = json.loads(self.state.read_text()); state['runs']['total_count'] = 101; self.state.write_text(json.dumps(state))
        self.assertEqual(self.check()['checks'][0]['state'], 'unknown')

    def test_notify_without_webhook_claims_no_acceptance_and_performs_no_send(self):
        self.check()
        result = self.cli('notify', '--report', self.report, '--receipt', self.receipt)
        self.assertEqual(result['state'], 'not_configured'); self.assertFalse(self.receipt.exists())
        env = {**self.env, 'CI_MONITOR_WEBHOOK': 'http://127.0.0.1:1/not-https'}
        self.cli('notify', '--report', self.report, '--receipt', self.receipt, success=False, env=env)
        self.assertFalse(self.receipt.exists())

    def test_explicit_https_notification_has_stable_idempotency_and_accepted_receipt(self):
        self.check(); server = self.webhook(); env = {**self.env, 'CI_MONITOR_WEBHOOK': server.url}
        accepted = self.cli('notify', '--report', self.report, '--receipt', self.receipt, env=env)
        self.assertEqual(accepted['state'], 'accepted'); self.assertEqual(len(server.events), 1)
        receipt = json.loads(self.receipt.read_text()); self.assertEqual(len(receipt['accepted']), 1)
        self.assertNotIn(server.url, self.receipt.read_text())
        self.assertEqual(server.events[0]['key'], accepted['idempotency_key'])
        # Observation time changes do not create a new incident or a second send.
        self.check('2026-11-12T21:30:00Z')
        repeated = self.cli('notify', '--report', self.report, '--receipt', self.receipt, env=env)
        self.assertEqual(repeated['state'], 'already_accepted'); self.assertEqual(len(server.events), 1)
        self.assertEqual(len(json.loads(self.receipt.read_text())['accepted']), 1)

    def test_failed_or_redirected_webhook_never_records_acceptance_and_retry_keeps_key(self):
        self.check(); server = self.webhook(); env = {**self.env, 'CI_MONITOR_WEBHOOK': server.url}
        server.status = 503
        self.cli('notify', '--report', self.report, '--receipt', self.receipt, env=env, success=False)
        self.assertFalse(self.receipt.exists()); self.assertFalse(self.receipt.with_name('receipt.json.lock').exists())
        first_key = server.events[0]['key']; server.status = 202
        accepted = self.cli('notify', '--report', self.report, '--receipt', self.receipt, env=env)
        self.assertEqual(accepted['idempotency_key'], first_key)
        # A distinct rerun incident is new, but a downgrade redirect is refused.
        self.set_state([self.run_record(run_attempt=2, conclusion='failure')]); self.check(); server.redirect = True
        self.cli('notify', '--report', self.report, '--receipt', self.receipt, env=env, success=False)
        self.assertEqual(len(json.loads(self.receipt.read_text())['accepted']), 1)

    def test_notification_never_spawns_gh_build_or_uploader_and_locked_receipt_fails_closed(self):
        self.check(); server = self.webhook()
        with mock.patch.dict(os.environ, {**self.env, 'CI_MONITOR_WEBHOOK': server.url}, clear=True), \
             mock.patch.object(monitor.subprocess, 'run', side_effect=AssertionError('notification must never execute a child')):
            self.assertEqual(monitor.notify(self.report, self.receipt)['state'], 'accepted')
        lock = self.receipt.with_name('receipt.json.lock'); lock.write_text('owned by another notification process')
        with mock.patch.dict(os.environ, {**self.env, 'CI_MONITOR_WEBHOOK': server.url}, clear=True):
            with self.assertRaises(monitor.MonitorError): monitor.notify(self.report, self.receipt)
        self.assertTrue(lock.exists()); self.assertEqual(len(server.events), 1)


if __name__ == '__main__': unittest.main()
