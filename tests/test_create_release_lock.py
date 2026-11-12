"""Actual lock-generator CLI against a corpus of real hash-validated image bundles."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'travis'))
import ci_evidence
import release_policy

SCRIPT = ROOT / 'travis/create_release_lock.py'
TAG = '6.1.1.8'
UPSTREAM = '81c2369'
FIELDS = {
    'aci-containers-host': 'aci_containers_host_version',
    'aci-containers-controller': 'aci_containers_controller_version',
    'cnideploy': 'cnideploy_version', 'aci-containers-operator': 'aci_containers_operator_version',
    'openvswitch': 'openvswitch_version', 'aci-containers-webhook': 'aci_containers_webhook_version',
    'aci-containers-certmanager': 'aci_containers_certmanager_version',
    'aci-containers-host-ovscni': 'aci_containers_host_ovscni_version', 'opflex': 'opflex_agent_version',
}
ACI = [name for name in FIELDS if name != 'opflex']


class CreateReleaseLockIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release-lock-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.cicd = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        self.calls = 0
        self.bundles = 0
        self.versions = self.dir / 'versions.yaml'
        self.write_versions()
        self.aci = self.bundle(ACI, 'noironetworks/aci-containers', 'b' * 40, '654321', '123')
        self.opflex = self.bundle(['opflex'], 'noironetworks/opflex', 'a' * 40, '987654', '127')
        self.base = f'quay.io/noirolabs/opflex-build-base:{TAG}.{UPSTREAM}=sha256:' + 'f' * 64

    def write_versions(self, missing=None, overrides=None):
        fields = {field: f'{TAG}.{UPSTREAM}' for field in FIELDS.values()}
        fields.update(overrides or {})
        if missing: del fields[missing]
        self.versions.write_text('versions:\n  6.1:\n' + ''.join(f'    {key}: {value}\n' for key, value in fields.items()))

    def bundle(self, names, repository, sha, run_id, number, attempt='2', release=TAG):
        self.bundles += 1
        target = self.dir / f'bundle-{self.bundles}'
        args = argparse.Namespace(bundle=str(target), release_tag=release, source_repository=repository,
                                 source_sha=sha, run_id=run_id, run_number=number, run_attempt=attempt,
                                 run_url=f'https://github.com/{repository}/actions/runs/{run_id}',
                                 trigger_tag=release, upstream_id=UPSTREAM)
        builder = ci_evidence.Builder(args, 'images', release + '.z', 'z')
        if repository in ('noironetworks/opflex', 'noironetworks/aci-containers'):
            builder.manifest['build_inputs'] = [{'role': 'opflex-build-base',
                'reference': 'quay.io/noirolabs/opflex-build-base@sha256:' + 'f' * 64,
                'digest': 'sha256:' + 'f' * 64, 'image_id': 'sha256:' + 'e' * 64,
                'captured_before_build': True, 'immutable': True}]

        for name in names:
            index = list(FIELDS).index(name) + 1
            digest = f'{index:064x}'
            root = f'release_artifacts/{release}/z/{name}/{release}'
            record = {'name': name, 'commit': [{'sha': sha, 'link': f'https://github.com/{repository}/commit/{sha}'}],
                      'sbom': root + '-sbom.txt', 'cve': root + '-cve.txt', 'build-logs': root + '-buildlog.txt',
                      'quay': [], 'docker': []}
            for report, key in [('sbom', 'sbom'), ('cve', 'cve'), ('buildlog', 'build-logs')]:
                source = self.dir / f'{self.bundles}-{name}-{report}.txt'
                source.write_text(f'Immutable {name} {report} fixture\n')
                builder.add(source, 'docs/' + record[key])
            base = release + '.' + UPSTREAM
            for registry, key in [('quay.io/noiro', 'quay'), ('noiro', 'docker')]:
                for tag in (base + '.z', base + '.111226.' + number):
                    record[key].append({'tag': tag, 'sha': digest, 'link': 'https://fixture.invalid/' + name})
                    builder.manifest['observations'].append({'reference': registry + '/' + name + ':' + tag,
                                                             'digest': 'sha256:' + digest})
            builder.manifest['records'].append(record)
            builder.manifest['images'].append({'name': name, 'image_id': 'sha256:' + digest})
        builder.finish()
        return target

    def cli(self, bundles=None, success=True, dependency=None, cicd=None, branch='mmr-6.1.1'):
        self.calls += 1
        output = self.dir / f'lock-{self.calls}.json'
        args = [sys.executable, str(SCRIPT), '--release-tag', TAG, '--source-branch', branch,
                '--cicd-commit', cicd or self.cicd, '--versions-file', str(self.versions), '--output', str(output)]
        for bundle in bundles or [self.aci, self.opflex]: args += ['--bundle', str(bundle)]
        for value in dependency if dependency is not None else [self.base]: args += ['--dependency', value]
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        if success:
            return json.loads(output.read_text())
        self.assertFalse(output.exists(), 'rejected cohort must not create an approval input')

    def alter(self, bundle, update):
        path = bundle / 'manifest.json'
        manifest = json.loads(path.read_text())
        update(manifest)
        path.write_text(json.dumps(manifest, sort_keys=True))

    def test_cli_creates_complete_lock_with_source_run_identity_and_observed_digest_constraints(self):
        for branch in ('mmr-6.1.1', 'master'):
            lock = self.cli(branch=branch)
            self.assertEqual(len(lock['images']), 9)
            self.assertEqual(lock['sources'], {'noironetworks/aci-containers': 'b' * 40, 'noironetworks/opflex': 'a' * 40})
            self.assertEqual(lock['images']['opflex']['producer']['run_id'], '987654')
            self.assertEqual(lock['images']['opflex']['producer']['run_attempt'], 2)
            self.assertEqual(lock['images']['opflex']['platform'], 'linux/amd64')
            for image, entry in lock['images'].items():
                for registry, item in entry['registries'].items():
                    self.assertEqual(item['reference'], f'{registry}/{image}:{TAG}.{UPSTREAM}')
                    self.assertFalse(item['reference'].endswith('.z'))
            self.assertNotIn('source_sha', lock); self.assertNotIn('tag_object', lock)
            # Schema compatibility only: these constraints are not observations
            # of promoted tags, which the production authorization collects later.
            expected = {item['reference']: item['digest'] for image in lock['images'].values() for item in image['registries'].values()}
            expected.update({item['reference']: item['digest'] for item in lock['dependencies']})
            release_policy.validate_input_lock(lock, TAG, {'schema_version': 1, 'digests': expected, 'platforms': {ref: 'linux/amd64' for ref in expected}})

    def test_cli_requires_recorded_immutable_compilation_base_matching_approved_dependency(self):
        original = json.loads((self.opflex / 'manifest.json').read_text())
        for update in (lambda m: m.pop('build_inputs'),
                       lambda m: m['build_inputs'][0].update(immutable=False),
                       lambda m: m['build_inputs'][0].update(captured_before_build=False),
                       lambda m: m['build_inputs'][0].update(digest='sha256:' + 'a' * 64)):
            manifest = json.loads(json.dumps(original)); update(manifest)
            (self.opflex / 'manifest.json').write_text(json.dumps(manifest))
            self.cli(success=False)
        (self.opflex / 'manifest.json').write_text(json.dumps(original))
        self.cli(success=False, dependency=[self.base.replace('f' * 64, 'a' * 64)])

    def test_cli_rejects_tampered_bundle_missing_image_pin_and_base_dependency(self):
        manifest = json.loads((self.aci / 'manifest.json').read_text())
        report = self.aci / 'artifacts' / manifest['files'][0]['path']
        original = report.read_bytes(); report.write_bytes(original + b'tampered')
        self.cli(success=False)
        report.write_bytes(original)
        incomplete = self.bundle(ACI[:-1], 'noironetworks/aci-containers', 'b' * 40, '654321', '123')
        self.cli([incomplete, self.opflex], success=False)
        self.write_versions(missing='opflex_agent_version'); self.cli(success=False)
        self.write_versions(); self.cli(success=False, dependency=[])
        self.cli(success=False, dependency=[self.base, self.base])
        self.cli(success=False, dependency=[self.base, f'quay.io/noiro/opflex:{TAG}.{UPSTREAM}=sha256:' + 'e' * 64])
        self.versions.write_text(self.versions.read_text() + '    opflex_agent_version: duplicate\n')
        self.cli(success=False)

    def test_cli_rejects_mixed_source_run_attempt_release_cicd_and_duplicate_ownership(self):
        left = self.bundle(ACI[:4], 'noironetworks/aci-containers', 'b' * 40, '654321', '123')
        for field, sha, run_id, number, attempt in [
            ('source', 'c' * 40, '654321', '123', '2'),
            ('run', 'b' * 40, '654322', '124', '2'),
            ('attempt', 'b' * 40, '654321', '123', '3'),
        ]:
            right = self.bundle(ACI[4:], 'noironetworks/aci-containers', sha, run_id, number, attempt)
            with self.subTest(field=field): self.cli([left, right, self.opflex], success=False)
        other_release = self.bundle(['opflex'], 'noironetworks/opflex', 'a' * 40, '987654', '127', release='6.1.1.9')
        self.cli([self.aci, other_release], success=False)
        self.cli(success=False, cicd='d' * 40)
        self.cli([self.aci, self.opflex, self.opflex], success=False)
        overlap = self.bundle([ACI[0]], 'noironetworks/aci-containers', 'b' * 40, '654321', '123')
        self.cli([self.aci, overlap, self.opflex], success=False)

    def test_cli_rejects_unobserved_or_conflicting_digests_unrelated_pins_and_legacy_run_identity(self):
        original = (self.opflex / 'manifest.json').read_text()
        mutations = [
            lambda m: m['records'][0]['quay'][0].update(sha='e' * 64),
            lambda m: m['observations'].clear(),
            lambda m: m['run'].pop('attempt'),
            lambda m: m['run'].update(id='not-an-api-run'),
            lambda m: m['records'][0]['commit'][0].update(sha='d' * 40),
            lambda m: m['observations'].append({**m['observations'][0], 'digest': 'sha256:' + 'e' * 64}),
        ]
        for index, mutation in enumerate(mutations):
            (self.opflex / 'manifest.json').write_text(original)
            self.alter(self.opflex, mutation)
            with self.subTest(mutation=index): self.cli(success=False)
        (self.opflex / 'manifest.json').write_text(original)
        self.write_versions(overrides={'opflex_agent_version': '6.1.1.8.unrelated'})
        self.cli(success=False)
        self.write_versions(overrides={'opflex_agent_version': '6.1.1.8.81c2369.z'})
        self.cli(success=False)
        self.write_versions(overrides={'opflex_agent_version': '6.1.1.8.81c2369.111226.127'})
        lock = self.cli()
        self.assertTrue(lock['images']['opflex']['registries']['quay.io/noiro']['reference'].endswith('.111226.127'))


if __name__ == '__main__': unittest.main()
