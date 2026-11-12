"""Offline release-policy integration: real Git/GPG/sdist, local index, stub uploader.

Run with Python 3.12: python -m unittest discover -s tests -p test_release_policy.py -v
Every key, remote, package and server belongs to a temporary directory. No user
keyring, real publication endpoint, registry, or production credential is used.
"""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'travis/release_policy.py'
spec = importlib.util.spec_from_file_location('release_policy_under_test', SCRIPT)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
TAG = '6.1.1.8'
SOURCE_BRANCH = 'mmr-6.1.1'
REPOSITORY = 'noironetworks/acc-provision'
WORKFLOW = 'acc-provision-tag-publish.yml'
JOB = 'Lint and unit tests'
PIN_KEYS = {
    'aci-containers-host': 'aci_containers_host_version',
    'aci-containers-controller': 'aci_containers_controller_version',
    'cnideploy': 'cnideploy_version',
    'aci-containers-operator': 'aci_containers_operator_version',
    'openvswitch': 'openvswitch_version',
    'aci-containers-webhook': 'aci_containers_webhook_version',
    'aci-containers-certmanager': 'aci_containers_certmanager_version',
    'aci-containers-host-ovscni': 'aci_containers_host_ovscni_version',
    'opflex': 'opflex_agent_version',
}


def run(argv, *, cwd=None, env=None, input_text=None, check=True):
    result = subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env,
                            input=input_text, capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise AssertionError(f'{Path(str(argv[0])).name} failed ({result.returncode}): {result.stderr}')
    return result


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    return path


class LocalIndex:
    """Read-only loopback index; the uploader fixture changes only its state file."""
    def __init__(self, directory):
        self.state = Path(directory) / 'index.json'
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.path)
                if not owner.state.exists():
                    self.send_error(404)
                    return
                body = owner.state.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ReleasePolicyIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix='rp-keys-', dir='/tmp')
        cls.addClassCleanup(cls.keys.cleanup)
        cls.keyhome = Path(cls.keys.name)
        cls.keyhome.chmod(0o700)
        cls.env = {**os.environ, 'GNUPGHOME': str(cls.keyhome), 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}
        cls.gpg = shutil.which('gpg')
        if not cls.gpg:
            raise RuntimeError('gpg is required for release-policy qualification')
        def generate(label):
            run([cls.gpg, '--batch', '--pinentry-mode', 'loopback', '--passphrase', '',
                 '--quick-generate-key', f'{label} <fixture-release@example.invalid>',
                 'ed25519', 'sign', '1d'], env=cls.env)
            listing = run([cls.gpg, '--batch', '--with-colons', '--list-secret-keys', label], env=cls.env).stdout
            return next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))
        cls.authorized = generate('Authorized Fixture')
        run([cls.gpg, '--batch', '--pinentry-mode', 'loopback', '--passphrase', '',
             '--quick-add-key', cls.authorized, 'ed25519', 'sign', '1d'], env=cls.env)
        listing = run([cls.gpg, '--batch', '--with-colons', '--list-secret-keys', cls.authorized], env=cls.env).stdout
        cls.subkey = [line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:')][1]
        cls.other = generate('Unauthorized Same Email Fixture')
        cls.public_data = run([cls.gpg, '--batch', '--armor', '--export', cls.authorized], env=cls.env).stdout

    @classmethod
    def tearDownClass(cls):
        # Do not leave an agent holding fixture keys or touch the user's agent.
        run(['gpgconf', '--homedir', str(cls.keyhome), '--kill', 'gpg-agent'], env=cls.env, check=False)
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rp-test-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.repo = self.dir / 'source'
        self.repo.mkdir()
        self.env = {**self.__class__.env}
        run(['git', 'init', '--quiet', '-b', SOURCE_BRANCH, self.repo], env=self.env)
        for name, value in [('user.name', 'Release Fixture'), ('user.email', 'fixture@example.invalid'),
                            ('gpg.program', self.gpg), ('commit.gpgsign', 'false')]:
            self.git('config', name, value)
        self.remote = self.dir / 'remote.git'
        run(['git', 'init', '--bare', '--quiet', self.remote], env=self.env)
        self.git('remote', 'add', 'origin', str(self.remote))
        self.current_cicd = run(['git', '-C', ROOT, 'rev-parse', 'HEAD'], env=self.env).stdout.strip()
        self.lock, self.observations = self.input_lock()
        self.lock_path = write_json(self.repo / 'ci/release-inputs.json', self.lock)
        self.public_key = self.repo / 'ci/release-signing-key.asc'
        self.public_key.write_text(self.public_data)
        (self.repo / 'README').write_text('Offline release fixture\n')
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'Create release fixture inputs')
        self.source = self.git('rev-parse', 'HEAD')
        self.policy = {'schema_version': 1, 'repository': REPOSITORY,
                       'fingerprint': self.authorized, 'email': 'fixture-release@example.invalid',
                       'source_branches': [SOURCE_BRANCH, 'master'],
                       'required_tests': [{'workflow': WORKFLOW, 'jobs': [JOB]}]}
        self.policy_path = write_json(self.dir / 'policy.json', self.policy)
        self.checks_path = write_json(self.dir / 'checks.json', self.check_evidence())
        self.observations_path = write_json(self.dir / 'observations.json', self.observations)
        self.artifact = self.build_sdist()
        self.manifest_path = self.dir / 'manifest.json'
        self.manifest = policy.package_manifest(self.artifact, 'acc_provision', TAG, 'pypi')
        write_json(self.manifest_path, self.manifest)

    def git(self, *args, input_text=None):
        return run(['git', '-C', self.repo, *args], env=self.env, input_text=input_text).stdout.strip()

    def cli(self, *args, success=True, env=None):
        result = run([sys.executable, SCRIPT, *args], env=env or self.env, check=False)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def input_lock(self):
        lock = {'schema_version': 1, 'release_tag': TAG, 'source_branch': SOURCE_BRANCH,
                'cicd_commit': self.current_cicd,
                'sources': {'noironetworks/opflex': 'a' * 40, 'noironetworks/aci-containers': 'b' * 40},
                'images': {}, 'dependencies': []}
        digests = {}
        for index, name in enumerate(policy.RUNTIME_IMAGES, 1):
            producer_repo = 'noironetworks/opflex' if name == 'opflex' else 'noironetworks/aci-containers'
            entry = {'platform': 'linux/amd64', 'producer': {'repository': producer_repo,
                     'source_sha': lock['sources'][producer_repo], 'cicd_commit': self.current_cicd,
                     'run_id': str(100 + index), 'run_attempt': 1}, 'registries': {}}
            for registry in ('quay.io/noiro', 'docker.io/noiro'):
                reference = f'{registry}/{name}:{TAG}.81c2369'
                digest = 'sha256:' + f'{index:064x}'
                entry['registries'][registry] = {'reference': reference, 'digest': digest}
                digests[reference] = digest
            lock['images'][name] = entry
        dependency = {'reference': f'quay.io/noirolabs/opflex-build-base:{TAG}.81c2369',
                      'digest': 'sha256:' + 'f' * 64}
        lock['dependencies'].append(dependency)
        digests[dependency['reference']] = dependency['digest']
        return lock, {'schema_version': 1, 'digests': digests}

    def check_evidence(self):
        required = {'repository': REPOSITORY, 'workflow': WORKFLOW, 'branch': SOURCE_BRANCH, 'jobs': [JOB]}
        return {'schema_version': 1, 'source_sha': self.source, 'requirements': [required],
                'runs': [{**{k: v for k, v in required.items() if k != 'jobs'}, 'source_sha': self.source,
                          'event': 'push', 'run_number': 12, 'run_attempt': 1, 'status': 'completed',
                          'conclusion': 'success', 'jobs': {JOB: 'success'}}]}

    def build_sdist(self, version=TAG, pins=None, name='acc_provision', destination=None):
        directory = destination or self.dir / ('package-' + str(len(list(self.dir.glob('package-*')))))
        source = directory / 'source'
        package = source / 'acc_provision'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        values = pins or {key: f'{TAG}.81c2369' for key in PIN_KEYS.values()}
        (package / 'versions.yaml').write_text('versions:\n  6.1:\n' + ''.join(f'    {key}: {value}\n' for key, value in values.items()))
        (source / 'setup.py').write_text(f"from setuptools import setup\nsetup(name={name!r}, version={version!r}, packages=['acc_provision'], package_data={{'acc_provision':['versions.yaml']}})\n")
        run([sys.executable, 'setup.py', 'sdist', '--dist-dir', directory / 'dist'], cwd=source, env=self.env)
        return next((directory / 'dist').glob('*.tar.gz'))

    def sign(self, key=None, message='Release to pypi.org', force=False):
        self.git('tag', '-s', '-u', (key or self.authorized) + '!', *(['-f'] if force else []), TAG, '-m', message)
        self.git('push', '--quiet', *(['--force'] if force else []), 'origin', 'refs/tags/' + TAG)

    def authorize(self, success=True):
        output = self.dir / 'authorization.json'
        self.cli('authorize', '--repo', self.repo, '--tag', TAG, '--policy', self.policy_path,
                 '--public-key', self.public_key, '--lock', self.lock_path, '--checks', self.checks_path,
                 '--observations', self.observations_path, '--artifact-manifest', self.manifest_path,
                 '--output', output, success=success)
        return json.loads(output.read_text()) if success else None

    def index_record(self, manifest=None):
        manifest = manifest or self.manifest
        return {'info': {'name': manifest['name'], 'version': manifest['version']},
                'urls': [{'filename': manifest['filename'], 'digests': {'sha256': manifest['sha256']}, 'yanked': False}]}

    def with_index(self):
        index = LocalIndex(self.dir)
        self.addCleanup(index.close)
        return index

    def preview_manifest(self):
        result = {**self.manifest, 'index': 'testpypi'}
        write_json(self.manifest_path, result)
        return result

    def test_cli_package_manifest_reads_real_sdist_metadata_and_hash(self):
        output = self.dir / 'cli-manifest.json'
        self.cli('package-manifest', '--artifact', self.artifact, '--name', 'acc-provision',
                 '--version', TAG, '--index', 'pypi', '--output', output)
        manifest = json.loads(output.read_text())
        self.assertEqual(manifest['name'], 'acc-provision')
        self.assertEqual(manifest['sha256'], hashlib.sha256(self.artifact.read_bytes()).hexdigest())
        self.assertEqual(manifest['size'], self.artifact.stat().st_size)

    def test_cli_manifest_rejects_name_version_symlink_and_duplicate_metadata(self):
        link = self.dir / self.artifact.name
        link.symlink_to(self.artifact)
        for artifact, name, version in [(self.artifact, 'other-package', TAG),
                                         (self.artifact, 'acc_provision', '6.1.1.9'),
                                         (link, 'acc_provision', TAG)]:
            with self.subTest(name=name, version=version, link=artifact.is_symlink()):
                self.cli('package-manifest', '--artifact', artifact, '--name', name, '--version', version,
                         '--index', 'pypi', '--output', self.dir / 'rejected.json', success=False)
        bad = self.dir / 'ambiguous.tar.gz'
        with tarfile.open(bad, 'w:gz') as archive:
            for prefix in ('one', 'two'):
                data = f'Name: acc_provision\nVersion: {TAG}\n'.encode()
                item = tarfile.TarInfo(prefix + '/PKG-INFO'); item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        with self.assertRaises(policy.PolicyError): policy.package_manifest(bad, 'acc_provision', TAG, 'pypi')

    def test_manifest_relocation_accepts_exact_bytes_and_rejects_modified_bytes(self):
        directory = self.dir / 'relocated'; directory.mkdir()
        relocated = directory / self.artifact.name
        shutil.copyfile(self.artifact, relocated)
        result = policy.validate_manifest(self.manifest, relocated)
        self.assertEqual(policy.manifest_identity(result), policy.manifest_identity(self.manifest))
        relocated.write_bytes(relocated.read_bytes() + b'changed')
        with self.assertRaises(policy.PolicyError): policy.validate_manifest(self.manifest, relocated)

    def test_cli_authorizes_genuine_approved_primary_signature(self):
        self.sign()
        authorization = self.authorize()
        self.assertEqual(authorization['source_sha'], self.source)
        self.assertEqual(authorization['tag_object'], self.git('rev-parse', 'refs/tags/' + TAG))
        self.assertEqual(authorization['package']['sha256'], self.manifest['sha256'])
        self.assertNotIn('artifact', authorization['package'])

    def test_authorized_signing_subkey_is_accepted(self):
        self.sign(self.subkey)
        identity = policy.verify_tag(self.repo, TAG, self.policy, self.public_key)
        self.assertEqual(identity['source_sha'], self.source)

    def test_same_email_with_unauthorized_key_is_rejected(self):
        self.sign(self.other)
        with self.assertRaises(policy.PolicyError): policy.verify_tag(self.repo, TAG, self.policy, self.public_key)
        other_public = self.dir / 'other-public.asc'
        other_public.write_text(run([self.gpg, '--batch', '--armor', '--export', self.other], env=self.env).stdout)
        with self.assertRaises(policy.PolicyError): policy.verify_tag(self.repo, TAG, self.policy, other_public)

    def test_unsigned_and_cryptographically_modified_tag_are_rejected(self):
        self.git('tag', '-a', TAG, '-m', 'Release to pypi.org')
        with self.assertRaises(policy.PolicyError): policy.verify_tag(self.repo, TAG, self.policy, self.public_key)
        self.sign(force=True)
        original = self.git('cat-file', 'tag', TAG)
        corrupt = original.replace('Release to pypi.org', 'Changed release to pypi.org', 1) + '\n'
        obj = self.git('hash-object', '-t', 'tag', '-w', '--stdin', input_text=corrupt)
        self.git('update-ref', 'refs/tags/' + TAG, obj)
        with self.assertRaises(policy.PolicyError): policy.verify_tag(self.repo, TAG, self.policy, self.public_key)

    def test_same_commit_signature_conversion_preserves_source_but_changes_tag_object(self):
        self.git('tag', '-a', TAG, '-m', 'Release to pypi.org')
        original = policy.tag_identity(self.repo, TAG)
        self.sign(force=True)
        signed = policy.verify_tag(self.repo, TAG, self.policy, self.public_key)
        self.assertEqual(original['source_sha'], signed['source_sha'])
        self.assertNotEqual(original['tag_object'], signed['tag_object'])

    def test_recheck_rejects_moved_local_tag_object_and_remote_only_mismatch(self):
        self.sign()
        identity = policy.tag_identity(self.repo, TAG)
        authorization = {'schema_version': 1, **identity, 'package': policy.manifest_identity(self.manifest)}
        policy.recheck(self.repo, authorization, self.manifest)
        original = identity['tag_object']
        self.sign(message='Re-signed release to pypi.org', force=True)
        with self.assertRaises(policy.PolicyError): policy.recheck(self.repo, authorization, self.manifest)
        # Restore only the local tag: the remote still names the newer annotated object.
        self.git('update-ref', 'refs/tags/' + TAG, original)
        with self.assertRaises(policy.PolicyError): policy.recheck(self.repo, authorization, self.manifest)

    def test_authorization_rejects_working_lock_edits_outside_signed_source(self):
        self.sign()
        changed = copy.deepcopy(self.lock); changed['source_branch'] = 'master'
        write_json(self.lock_path, changed)
        self.authorize(success=False)
        write_json(self.lock_path, self.lock)
        forged = self.check_evidence()
        forged['requirements'][0]['workflow'] = 'attacker.yml'
        forged['runs'][0]['workflow'] = 'attacker.yml'
        write_json(self.checks_path, forged)
        # Authorization must use the trusted policy requirements, not the claim in evidence.
        self.authorize(success=False)

    def test_authorization_rejects_stale_running_cicd_input(self):
        changed = copy.deepcopy(self.lock); changed['cicd_commit'] = 'd' * 40
        for image in changed['images'].values(): image['producer']['cicd_commit'] = changed['cicd_commit']
        write_json(self.lock_path, changed)
        self.git('add', 'ci/release-inputs.json'); self.git('commit', '--quiet', '-m', 'Lock stale CICD fixture')
        self.source = self.git('rev-parse', 'HEAD'); write_json(self.checks_path, self.check_evidence())
        self.sign()
        self.authorize(success=False)

    def test_authorization_rejects_package_image_pins_different_from_locked_inputs(self):
        pins = {key: f'{TAG}.81c2369' for key in PIN_KEYS.values()}
        pins['opflex_agent_version'] = f'{TAG}.different'
        artifact = self.build_sdist(pins=pins)
        write_json(self.manifest_path, policy.package_manifest(artifact, 'acc_provision', TAG, 'pypi'))
        self.sign()
        self.authorize(success=False)

    def test_exact_branch_push_latest_attempt_and_required_job_must_succeed(self):
        original = self.check_evidence()
        policy.validate_checks(original, self.source)
        for mutation in [
            {'event': 'pull_request'}, {'source_sha': 'e' * 40}, {'branch': 'other'},
            {'workflow': 'untrusted.yml'}, {'repository': 'fork/acc-provision'},
            {'status': 'in_progress', 'conclusion': None},
            {'status': 'completed', 'conclusion': 'skipped'}, {'jobs': {JOB: 'skipped'}},
        ]:
            candidate = copy.deepcopy(original); candidate['runs'][0].update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(policy.PolicyError): policy.validate_checks(candidate, self.source)
        latest = copy.deepcopy(original['runs'][0]); latest.update(run_attempt=2, status='in_progress', conclusion=None)
        original['runs'].append(latest)
        with self.assertRaises(policy.PolicyError): policy.validate_checks(original, self.source)

    def test_collect_checks_uses_exact_workflow_branch_attempt_and_canonical_repository(self):
        base = {'id': 51, 'head_sha': self.source, 'event': 'push', 'head_branch': SOURCE_BRANCH,
                'head_repository': {'full_name': REPOSITORY}, 'run_attempt': 3, 'run_number': 9,
                'status': 'completed', 'conclusion': 'success', 'path': '.github/workflows/' + WORKFLOW}
        payload = {'workflow_runs': [base, {**base, 'id': 52, 'path': '.github/workflows/other.yml'},
                    {**base, 'id': 53, 'event': 'pull_request'},
                    {**base, 'id': 54, 'head_repository': {'full_name': 'fork/acc-provision'}}]}
        requests = []
        def response(url, **kwargs):
            requests.append(url)
            return payload if '/workflows/' in url else {'total_count': 1, 'jobs': [{'name': JOB, 'conclusion': 'success'}]}
        with mock.patch.object(policy, 'http_json', side_effect=response):
            result = policy.collect_checks(REPOSITORY, self.source, SOURCE_BRANCH, WORKFLOW, [JOB])
        self.assertEqual([r['id'] for r in result['runs']], [51])
        self.assertIn('/attempts/3/jobs?', requests[1])
        policy.validate_checks(result, self.source)

    def test_complete_nine_image_lock_plus_base_rejects_missing_or_changed_inputs(self):
        self.assertEqual(len(self.lock['images']), 9)
        policy.validate_input_lock(self.lock, TAG, self.observations)
        changed = copy.deepcopy(self.observations)
        ref = next(iter(changed['digests'])); changed['digests'][ref] = 'sha256:' + '0' * 64
        with self.assertRaises(policy.PolicyError): policy.validate_input_lock(self.lock, TAG, changed)
        for kind in ('missing-image', 'missing-base', 'wrong-platform', 'floating-z', 'wrong-producer'):
            lock = copy.deepcopy(self.lock)
            if kind == 'missing-image': del lock['images']['opflex']
            elif kind == 'missing-base': lock['dependencies'] = []
            elif kind == 'wrong-platform': lock['images']['opflex']['platform'] = 'linux/arm64'
            elif kind == 'floating-z': lock['images']['opflex']['registries']['quay.io/noiro']['reference'] += '.z'
            else: lock['images']['opflex']['producer']['source_sha'] = 'e' * 40
            with self.subTest(kind=kind):
                with self.assertRaises(policy.PolicyError): policy.validate_input_lock(lock, TAG, self.observations)

    def test_cli_inspect_inputs_uses_read_only_docker_without_publication_secrets(self):
        executable = self.dir / 'bin'; executable.mkdir()
        capture = self.dir / 'docker-calls.jsonl'
        state = write_json(self.dir / 'docker-state.json', self.observations['digests'])
        script = executable / 'docker'
        script.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
            'with open(os.environ["FIXTURE_CAPTURE"],"a") as stream: stream.write(json.dumps({"argv":sys.argv[1:],"secret_names":[k for k in os.environ if k in ("TRAVIS_TAGGER","PYPI_PASS","TWINE_PASSWORD")]})+"\\n")\n'
            'print(json.dumps({"digest":json.load(open(os.environ["FIXTURE_STATE"]))[sys.argv[-1]]}))\n')
        script.chmod(0o755)
        env = {**self.env, 'PATH': str(executable) + os.pathsep + self.env['PATH'],
               'FIXTURE_CAPTURE': str(capture), 'FIXTURE_STATE': str(state),
               'TRAVIS_TAGGER': 'fixture-value', 'PYPI_PASS': 'fixture-value', 'TWINE_PASSWORD': 'fixture-value'}
        output = self.dir / 'observed.json'
        self.cli('inspect-inputs', '--lock', self.lock_path, '--output', output, env=env)
        self.assertEqual(json.loads(output.read_text()), self.observations)
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        self.assertEqual(len(calls), 19)
        self.assertTrue(all(c['argv'][:3] == ['buildx', 'imagetools', 'inspect'] and not c['secret_names'] for c in calls))

    def test_existing_verified_package_recreates_receipt_without_credentials_build_or_upload(self):
        manifest = self.preview_manifest(); index = self.with_index(); write_json(index.state, self.index_record(manifest))
        receipt = self.dir / 'receipt.json'
        with mock.patch.dict(policy.INDEXES, {'testpypi': (index.url, index.url + '/legacy/')}), \
             mock.patch.object(policy, 'command', side_effect=AssertionError('repair must not spawn build/upload')), \
             mock.patch.dict(os.environ, {}, clear=True):
            result = policy.main(['upload', '--manifest', str(self.manifest_path), '--receipt', str(receipt)])
        self.assertEqual(result, 0)
        retained = json.loads(receipt.read_text())
        self.assertTrue(retained['verified']); self.assertTrue(retained['reused_existing'])
        self.assertEqual(retained['package']['sha256'], manifest['sha256'])
        self.assertTrue(index.requests)

    def test_existing_package_hash_difference_or_yanked_file_prevents_upload(self):
        manifest = self.preview_manifest(); index = self.with_index()
        for kind in ('hash', 'yanked', 'identity'):
            record = self.index_record(manifest)
            if kind == 'hash': record['urls'][0]['digests']['sha256'] = '0' * 64
            elif kind == 'yanked': record['urls'][0]['yanked'] = True
            else: record['info']['name'] = 'different-package'
            write_json(index.state, record)
            with self.subTest(kind=kind), mock.patch.dict(policy.INDEXES, {'testpypi': (index.url, index.url + '/legacy/')}), \
                 mock.patch.object(policy, 'command', side_effect=AssertionError('must not upload conflicting bytes')):
                with self.assertRaises(policy.PolicyError): policy.upload(self.manifest_path, self.dir / 'receipt.json')

    def test_lost_upload_ack_reconciles_exact_bytes_and_child_gets_only_selected_credentials(self):
        manifest = self.preview_manifest(); index = self.with_index()
        modules = self.dir / 'modules'; twine = modules / 'twine'; twine.mkdir(parents=True)
        (twine / '__init__.py').write_text('')
        capture = self.dir / 'uploader.json'; expected = write_json(self.dir / 'expected-index.json', self.index_record(manifest))
        (twine / '__main__.py').write_text('''import hashlib,json,os,pathlib,sys
artifact=pathlib.Path(sys.argv[-1])
if sys.argv[1] != 'upload': raise SystemExit(91)
allowed={'TWINE_USERNAME','TWINE_PASSWORD','TWINE_NON_INTERACTIVE'}
secret_names=sorted(k for k in os.environ if k in {'TRAVIS_TAGGER','GITHUB_TOKEN','PYPI_PASS','TEST_PYPI_PASS','TWINE_PASSWORD','TWINE_USERNAME'})
pathlib.Path(os.environ['FIXTURE_CAPTURE']).write_text(json.dumps({'secret_names':secret_names,'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),'mode':artifact.stat().st_mode & 0o777,'argv':sys.argv[1:]}))
pathlib.Path(os.environ['FIXTURE_INDEX_STATE']).write_bytes(pathlib.Path(os.environ['FIXTURE_EXPECTED_INDEX']).read_bytes())
raise SystemExit(23)
''')
        env = {**self.env, 'PYTHONPATH': str(modules), 'FIXTURE_CAPTURE': str(capture),
               'FIXTURE_INDEX_STATE': str(index.state), 'FIXTURE_EXPECTED_INDEX': str(expected),
               'TWINE_USERNAME': '__token__', 'TWINE_PASSWORD': 'pypi-fixture-only',
               'TRAVIS_TAGGER': 'fixture-value', 'GITHUB_TOKEN': 'fixture-value',
               'PYPI_PASS': 'fixture-value', 'TEST_PYPI_PASS': 'fixture-value'}
        receipt = self.dir / 'receipt.json'
        with mock.patch.dict(policy.INDEXES, {'testpypi': (index.url, index.url + '/legacy/')}), \
             mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(policy.main(['upload', '--manifest', str(self.manifest_path), '--receipt', str(receipt)]), 0)
        child = json.loads(capture.read_text())
        self.assertEqual(child['secret_names'], ['TWINE_PASSWORD', 'TWINE_USERNAME'])
        self.assertEqual(child['sha256'], manifest['sha256'])
        self.assertEqual(child['mode'], 0o400)
        self.assertTrue(json.loads(receipt.read_text())['verified'])
        self.assertFalse(json.loads(receipt.read_text())['reused_existing'])
        self.assertEqual(hashlib.sha256(self.artifact.read_bytes()).hexdigest(), manifest['sha256'])

    def test_production_upload_reverifies_signature_policy_remote_tag_and_relocated_bytes(self):
        self.sign(); authorization = self.authorize()
        authorization_path = write_json(self.dir / 'authorization.json', authorization)
        moved = self.dir / 'download'; moved.mkdir(); artifact = moved / self.artifact.name; shutil.copyfile(self.artifact, artifact)
        index = self.with_index(); write_json(index.state, self.index_record())
        receipt = self.dir / 'production-receipt.json'
        with mock.patch.dict(policy.INDEXES, {'pypi': (index.url, index.url + '/legacy/')}):
            result = policy.main(['upload', '--manifest', str(self.manifest_path), '--artifact', str(artifact),
                '--receipt', str(receipt), '--authorization', str(authorization_path), '--repo', str(self.repo),
                '--policy', str(self.policy_path), '--public-key', str(self.public_key)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(receipt.read_text())['tag_object'], authorization['tag_object'])
        authorization['tag_object'] = 'e' * 40; write_json(authorization_path, authorization)
        with mock.patch.object(policy, 'existing_package', side_effect=AssertionError('must reject before index access')):
            with self.assertRaises(policy.PolicyError): policy.upload(self.manifest_path, receipt, authorization_path,
                self.repo, artifact, self.policy_path, self.public_key)


if __name__ == '__main__':
    unittest.main()
