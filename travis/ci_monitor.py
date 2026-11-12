#!/usr/bin/env python3
"""Bounded read-only release-run checks and separately invoked owner notifications."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_REQUESTS = 20
MAX_RECEIPTS = 1000
MAX_JSON = 2 * 1024 * 1024
ACTIONABLE = {'missed', 'disabled', 'wrong-source', 'failed'}
SHA = re.compile(r'[0-9a-f]{40}\Z')
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
TAG = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\Z')
WORKFLOW = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml\Z')


class MonitorError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise urllib.error.HTTPError(request.full_url, code, 'notification redirects are disabled', headers, fp)


def timestamp(value):
    if not isinstance(value, str):
        raise MonitorError('timestamps must explicitly include a timezone')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise MonitorError('invalid explicit timestamp') from error
    if result.tzinfo is None:
        raise MonitorError('timestamps must explicitly include a timezone')
    return result.astimezone(timezone.utc)


def now_text(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
        raise MonitorError('JSON input must be a bounded regular file')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise MonitorError('JSON input is missing or malformed') from error


def write_json(path, value):
    target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=target.parent, delete=False, encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, indent=2); stream.write('\n'); temporary = Path(stream.name)
    temporary.replace(target)


def gh_environment():
    # A read-only GitHub credential may reach gh; status, package, registry and
    # notification credentials never reach the observation child.
    allowed = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
               'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'GH_HOST', 'GH_CONFIG_DIR',
               'GH_TOKEN', 'GITHUB_TOKEN'}
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result.update(GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1', GIT_TERMINAL_PROMPT='0')
    return result


def gh_json(path, fields, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MonitorError('observation time budget exhausted')
    args = ['gh', 'api', '--method', 'GET', path]
    for key, value in fields.items():
        args += ['--raw-field', key + '=' + str(value)]
    try:
        result = subprocess.run(args, env=gh_environment(), capture_output=True, text=True,
                                timeout=min(10, remaining), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MonitorError('GitHub observation unavailable or timed out') from error
    if result.returncode or len(result.stdout.encode('utf-8')) > MAX_JSON:
        raise MonitorError('GitHub observation unavailable or exceeds the response budget')
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise MonitorError('GitHub observation was malformed') from error
    if not isinstance(value, dict):
        raise MonitorError('GitHub observation must be an object')
    return value


def watched_request(value):
    if not isinstance(value, dict):
        raise MonitorError('watched release request must be an object')
    result = {field: value.get(field) for field in ('repository', 'tag', 'source_sha', 'requested_at', 'deadline', 'workflow')}
    for field, regex in [('repository', REPOSITORY), ('tag', TAG), ('source_sha', SHA), ('workflow', WORKFLOW)]:
        if not isinstance(result[field], str) or not regex.fullmatch(result[field]):
            raise MonitorError('watched release request has an invalid explicit identity')
    requested, deadline = timestamp(result['requested_at']), timestamp(result['deadline'])
    if deadline < requested:
        raise MonitorError('release-run deadline precedes the explicit request')
    result['requested_at'], result['deadline'] = now_text(requested), now_text(deadline)
    return result


def check_request(request, now, budget):
    result = {'request': request}
    try:
        path = 'repos/' + request['repository'] + '/actions/workflows/' + request['workflow']
        workflow = gh_json(path, {}, budget)
        if workflow.get('state') != 'active':
            state = workflow.get('state')
            if isinstance(state, str) and state.startswith('disabled'):
                return {**result, 'state': 'disabled', 'reason': 'watched workflow is disabled'}
            raise MonitorError('watched workflow state is unavailable')
        payload = gh_json(path + '/runs', {'event': 'push', 'branch': request['tag'], 'per_page': 100}, budget)
        runs = payload.get('workflow_runs')
        if not isinstance(runs, list) or not isinstance(payload.get('total_count'), int) or payload['total_count'] > 100:
            raise MonitorError('run history is malformed or exceeds the bounded query')
        requested = timestamp(request['requested_at'])
        candidates = []
        for run in runs:
            if not isinstance(run, dict):
                raise MonitorError('run history contains a malformed entry')
            if (run.get('event') != 'push' or run.get('head_branch') != request['tag']
                    or run.get('head_repository', {}).get('full_name') != request['repository']
                    or str(run.get('path', '')).split('@', 1)[0] != '.github/workflows/' + request['workflow']):
                continue
            created = timestamp(run.get('created_at'))
            if created < requested or created > now:
                continue
            if (not isinstance(run.get('id'), int) or run['id'] < 1
                    or not isinstance(run.get('run_attempt'), int) or run['run_attempt'] < 1
                    or not SHA.fullmatch(str(run.get('head_sha', '')))):
                raise MonitorError('matching run identity is incomplete')
            candidates.append((created, run['id'], run['run_attempt'], run))
        if not candidates:
            state = 'missed' if now >= timestamp(request['deadline']) else 'waiting'
            return {**result, 'state': state, 'reason': 'no matching workflow/tag push run since the explicit request'}
        # A newer wrong-source attempt must not be hidden by an older success.
        run = max(candidates, key=lambda item: item[:3])[3]
        selected = {key: run.get(key) for key in ('id', 'run_attempt', 'head_sha', 'created_at', 'status', 'conclusion', 'html_url')}
        result.update(run=selected, started_late=timestamp(run['created_at']) > timestamp(request['deadline']))
        if run['head_sha'] != request['source_sha']:
            return {**result, 'state': 'wrong-source', 'reason': 'latest watched tag run has a different source SHA'}
        if run.get('status') in ('queued', 'in_progress', 'waiting', 'pending', 'requested'):
            return {**result, 'state': 'pending', 'reason': 'matching release run has not completed'}
        if run.get('status') != 'completed':
            raise MonitorError('matching run has an unknown execution status')
        if run.get('conclusion') == 'success':
            return {**result, 'state': 'healthy', 'reason': 'matching release workflow completed successfully'}
        if run.get('conclusion') in ('failure', 'cancelled', 'timed_out', 'action_required', 'stale', 'startup_failure', 'skipped', 'neutral'):
            return {**result, 'state': 'failed', 'reason': 'matching release workflow did not succeed'}
        raise MonitorError('completed matching run has no recognized conclusion')
    except (MonitorError, TypeError, AttributeError) as error:
        return {**result, 'state': 'unknown', 'reason': str(error)}


def check(requests_path=None, now=None, budget_seconds=60):
    now = now or datetime.now(timezone.utc)
    report = {'schema_version': 1, 'observed_at': now_text(now), 'checks': []}
    if not requests_path:
        return {**report, 'state': 'unknown', 'reason': 'watched release requests are not configured'}
    try:
        config = load_json(requests_path)
        requests = config.get('requests') if isinstance(config, dict) else None
        if config.get('schema_version') != 1 or not isinstance(requests, list) or not requests:
            raise MonitorError('watched release requests are empty or malformed')
        if len(requests) > MAX_REQUESTS:
            raise MonitorError('watched release requests exceed the observation budget')
        requests = [watched_request(value) for value in requests]
        identities = [json.dumps(value, sort_keys=True) for value in requests]
        if len(identities) != len(set(identities)):
            raise MonitorError('duplicate watched release request')
    except (MonitorError, OSError, AttributeError) as error:
        return {**report, 'state': 'unknown', 'reason': str(error)}
    budget = time.monotonic() + budget_seconds
    report['checks'] = [check_request(request, now, budget) for request in requests]
    states = {entry['state'] for entry in report['checks']}
    report['state'] = 'alert' if states & ACTIONABLE else 'unknown' if 'unknown' in states else 'healthy' if states == {'healthy'} else 'pending'
    return report


def notification(report):
    if not isinstance(report, dict) or report.get('schema_version') != 1 or not isinstance(report.get('checks'), list):
        raise MonitorError('notification requires a monitor report')
    alerts = []
    for entry in report['checks']:
        if not isinstance(entry, dict):
            raise MonitorError('notification report contains a malformed check')
        if entry.get('state') not in ACTIONABLE:
            continue
        request = watched_request(entry.get('request'))
        alert = {'request': request, 'state': entry['state']}
        if isinstance(entry.get('run'), dict):
            alert['run'] = {key: entry['run'].get(key) for key in ('id', 'run_attempt', 'head_sha', 'conclusion')}
        alerts.append(alert)
    alerts.sort(key=lambda entry: json.dumps(entry, sort_keys=True))
    canonical = json.dumps(alerts, sort_keys=True, separators=(',', ':')).encode()
    return {'schema_version': 1, 'idempotency_key': hashlib.sha256(canonical).hexdigest(), 'alerts': alerts}


def notify(report_path, receipt_path, webhook_env='CI_MONITOR_WEBHOOK'):
    payload = notification(load_json(report_path))
    if not payload['alerts']:
        return {'state': 'no_alerts', 'reason': 'report contains no actionable release-run failure'}
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*', webhook_env):
        raise MonitorError('webhook setting must name an environment variable')
    webhook = os.environ.get(webhook_env)
    if not webhook:
        return {'state': 'not_configured', 'reason': 'notification webhook is not configured'}
    endpoint = urllib.parse.urlsplit(webhook)
    if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.fragment:
        raise MonitorError('owner notification endpoint must be HTTPS without user information or fragment')
    destination = hashlib.sha256(webhook.encode()).hexdigest()
    receipt_path = Path(receipt_path); receipt_path.parent.mkdir(parents=True, exist_ok=True)
    lock = receipt_path.with_name(receipt_path.name + '.lock')
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise MonitorError('notification receipt is already in use') from error
    os.close(descriptor)
    try:
        receipts = load_json(receipt_path) if receipt_path.exists() else {'schema_version': 1, 'accepted': []}
        if not isinstance(receipts, dict) or receipts.get('schema_version') != 1 or not isinstance(receipts.get('accepted'), list) or any(not isinstance(item, dict) for item in receipts['accepted']):
            raise MonitorError('notification receipt history is malformed')
        if any(item.get('idempotency_key') == payload['idempotency_key'] and item.get('webhook_fingerprint') == destination for item in receipts['accepted']):
            return {'state': 'already_accepted', 'idempotency_key': payload['idempotency_key']}
        if len(receipts['accepted']) >= MAX_RECEIPTS:
            raise MonitorError('notification receipt history is full; owner rotation is required')
        request = urllib.request.Request(webhook, data=json.dumps(payload, sort_keys=True).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'Idempotency-Key': payload['idempotency_key'], 'User-Agent': 'noironetworks-ci-monitor'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
                status = response.status
                if not 200 <= status < 300:
                    raise MonitorError('notification endpoint did not accept the request')
        except (OSError, urllib.error.HTTPError) as error:
            raise MonitorError('notification endpoint did not acknowledge acceptance') from error
        receipts['accepted'].append({'idempotency_key': payload['idempotency_key'], 'webhook_fingerprint': destination,
                                     'accepted_at': now_text(), 'http_status': status})
        write_json(receipt_path, receipts)
        return {'state': 'accepted', 'idempotency_key': payload['idempotency_key'], 'http_status': status}
    finally:
        lock.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    observe = subs.add_parser('check')
    observe.add_argument('--requests')
    observe.add_argument('--output', required=True)
    observe.add_argument('--now', help='explicit ISO timestamp for replay/qualification')
    send = subs.add_parser('notify')
    send.add_argument('--report', required=True); send.add_argument('--receipt', required=True)
    send.add_argument('--webhook-env', default='CI_MONITOR_WEBHOOK')
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            value = check(args.requests, timestamp(args.now) if args.now else None)
            write_json(args.output, value)
        else:
            value = notify(args.report, args.receipt, args.webhook_env)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (MonitorError, OSError, ValueError, TypeError, KeyError) as error:
        print('CI monitor rejected: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
