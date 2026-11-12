#!/usr/bin/env python3
"""Create a reviewable ACC input lock from retained, hash-validated image bundles.

Bundle z/dated observations supply the expected digests. Consumer references come
from the production versions.yaml, including an explicit promotion target when
it is the bundle's release/upstream tag. This command never asserts that a
promotion has happened: release_policy.py inspect-inputs must independently
observe those consumer references before production authorization.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import tempfile

import yaml

from ci_evidence import EvidenceError, validate_bundle

SHA = re.compile(r'[0-9a-f]{40}\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
TAG = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\Z')
POSITIVE = re.compile(r'[1-9][0-9]*\Z')
FIELDS = {
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


class LockError(ValueError):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise LockError('versions file contains duplicate YAML keys')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def production_pins(path, release):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise LockError('versions file must be a bounded regular file')
    data = yaml.load(path.read_text(encoding='utf-8'), Loader=UniqueKeyLoader)
    if not isinstance(data, dict) or not isinstance(data.get('versions'), dict):
        raise LockError('versions file has no release series')
    series = '.'.join(release.split('.')[:2])
    rows = [value for key, value in data['versions'].items() if str(key) == series]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise LockError('versions file has no unambiguous active release series')
    pins = {}
    for name, field in FIELDS.items():
        value = rows[0].get(field)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value) or value.endswith('.z'):
            raise LockError('production versions file is missing a fixed runtime image pin')
        pins[name] = value
    return pins


def normalized_reference(reference):
    if not isinstance(reference, str):
        raise LockError('invalid observation reference')
    return 'docker.io/' + reference if reference.startswith('noiro/') else reference


def observed_digests(manifest):
    observations = {}
    for entry in manifest['observations']:
        reference = normalized_reference(entry['reference'])
        digest = entry['digest']
        if not DIGEST.fullmatch(str(digest)):
            raise LockError('invalid bundle observation digest')
        if reference in observations and observations[reference] != digest:
            raise LockError('bundle contains conflicting digest observations')
        observations[reference] = digest
    return observations


def consumer_digest(record, registry, pin, base_tag, number, observations):
    key = 'quay' if registry == 'quay.io/noiro' else 'docker'
    records = record.get(key)
    if not isinstance(records, list) or len(records) != 2:
        raise LockError('each image must retain one z and one dated registry result')
    tags = {}
    for item in records:
        tag, digest = item.get('tag'), 'sha256:' + str(item.get('sha', ''))
        if not isinstance(tag, str) or tag in tags or not DIGEST.fullmatch(digest):
            raise LockError('duplicate or invalid registry record')
        reference = registry + '/' + record['name'] + ':' + tag
        if observations.get(reference) != digest:
            raise LockError('registry record has no matching published digest observation')
        tags[tag] = digest
    z_tag = base_tag + '.z'
    dated = [tag for tag in tags if re.fullmatch(re.escape(base_tag) + r'\.[0-9]{6}\.' + re.escape(number), tag)]
    if z_tag not in tags or len(dated) != 1 or tags[z_tag] != tags[dated[0]]:
        raise LockError('published z and dated results do not identify one image')
    if pin == base_tag:
        return tags[z_tag]
    if pin in tags and pin != z_tag:
        return tags[pin]
    raise LockError('consumer pin cannot be derived from the retained published cohort')


def dependencies(values):
    result = {}
    has_base = False
    for value in values:
        reference, separator, digest = value.rpartition('=')
        if (not separator or not re.fullmatch(r'(?:quay\.io|docker\.io)/[A-Za-z0-9_./:@-]+', reference)
                or not DIGEST.fullmatch(digest) or reference.endswith('.z')
                or (':' not in reference.rsplit('/', 1)[-1] and '@' not in reference)):
            raise LockError('dependency requires an explicit public reference and sha256 digest')
        if '@' in reference and reference.rsplit('@', 1)[-1] != digest:
            raise LockError('dependency reference and digest disagree')
        if reference in result:
            raise LockError('duplicate dependency is ambiguous')
        result[reference] = digest
        has_base |= reference.split('@', 1)[0].split(':', 1)[0].endswith('/opflex-build-base')
    if not has_base:
        raise LockError('the opflex-build-base dependency must be explicitly locked')
    return [{'reference': reference, 'digest': result[reference]} for reference in sorted(result)]


def create_lock(bundle_paths, release_tag, source_branch, cicd_commit, versions_file, dependency_values):
    if not TAG.fullmatch(release_tag) or not SHA.fullmatch(cicd_commit):
        raise LockError('release tag and full CICD SHA are required')
    if source_branch not in ('master', 'mmr-' + '.'.join(release_tag.split('.')[:3])):
        raise LockError('source branch must identify the production master or MMR line')
    pins = production_pins(versions_file, release_tag)
    cohorts, images, sources, bundle_roots = {}, {}, {}, set()
    upstream_ids = set()
    for path in bundle_paths:
        path = Path(path)
        if path.resolve() in bundle_roots:
            raise LockError('a bundle was supplied more than once')
        bundle_roots.add(path.resolve())
        manifest, _ = validate_bundle(path)
        if (manifest['kind'] != 'images' or manifest['directory'] != 'z'
                or manifest['release_tag'] != release_tag or manifest['cicd']['sha'] != cicd_commit):
            raise LockError('bundle release, kind, or CICD revision differs from the requested cohort')
        source = manifest['source']; repository = source['repository']; source_sha = source['sha']
        if repository not in ('noironetworks/aci-containers', 'noironetworks/opflex') or not SHA.fullmatch(source_sha):
            raise LockError('bundle must identify a canonical runtime-image producer')
        run = manifest['run']
        if any(not POSITIVE.fullmatch(str(run.get(field, ''))) for field in ('id', 'number', 'attempt')):
            raise LockError('bundle needs distinct positive API run ID, number, and attempt')
        if run.get('trigger_tag') != release_tag or not re.fullmatch(r'[0-9a-f]+', str(run.get('upstream_id', ''))):
            raise LockError('bundle trigger or upstream identity is inconsistent')
        upstream_ids.add(run['upstream_id'])
        cohort = (source_sha, str(run['id']), str(run['number']), int(run['attempt']))
        if repository in cohorts and cohorts[repository] != cohort:
            raise LockError('producer bundles mix source commits or rerun attempts')
        cohorts[repository] = cohort; sources[repository] = source_sha
        observations = observed_digests(manifest)
        image_ids = [item['name'] for item in manifest['images']]
        records = manifest['records']
        if len(image_ids) != len(set(image_ids)) or set(image_ids) != {record['name'] for record in records}:
            raise LockError('bundle image identities do not match its records')
        base_tag = release_tag + '.' + run['upstream_id']
        for record in records:
            name = record['name']
            expected_repo = 'noironetworks/opflex' if name == 'opflex' else 'noironetworks/aci-containers'
            if name not in FIELDS or repository != expected_repo or name in images:
                raise LockError('unexpected, duplicate, or ambiguously owned runtime image')
            commits = record.get('commit', [])
            if (len(commits) != 1 or commits[0].get('sha') != source_sha
                    or commits[0].get('link') != 'https://github.com/' + repository + '/commit/' + source_sha):
                raise LockError('image record source differs from its producing bundle')
            registries = {}
            for registry in ('quay.io/noiro', 'docker.io/noiro'):
                registries[registry] = {'reference': registry + '/' + name + ':' + pins[name],
                    'digest': consumer_digest(record, registry, pins[name], base_tag, str(run['number']), observations)}
            images[name] = {'platform': 'linux/amd64',
                'producer': {'repository': repository, 'source_sha': source_sha, 'cicd_commit': cicd_commit,
                             'run_id': str(run['id']), 'run_attempt': int(run['attempt'])},
                'registries': registries}
    if set(images) != set(FIELDS) or set(sources) != {'noironetworks/aci-containers', 'noironetworks/opflex'} or len(upstream_ids) != 1:
        raise LockError('one complete nine-image release/upstream cohort is required')
    locked_dependencies = dependencies(dependency_values)
    consumer_refs = {item['reference'] for entry in images.values() for item in entry['registries'].values()}
    if any(item['reference'] in consumer_refs for item in locked_dependencies):
        raise LockError('dependency cannot duplicate a runtime consumer constraint')
    return {'schema_version': 1, 'release_tag': release_tag, 'source_branch': source_branch,
            'cicd_commit': cicd_commit, 'sources': sources,
            'images': {name: images[name] for name in sorted(images)},
            'dependencies': locked_dependencies}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', action='append', required=True)
    for flag in ('release-tag', 'source-branch', 'cicd-commit', 'versions-file', 'output'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--dependency', action='append', required=True, help='REF=sha256:DIGEST; must include opflex-build-base')
    args = parser.parse_args(argv)
    try:
        result = create_lock(args.bundle, args.release_tag, args.source_branch, args.cicd_commit,
                             args.versions_file, args.dependency)
        output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', dir=output.parent, delete=False, encoding='utf-8') as stream:
            json.dump(result, stream, indent=2, sort_keys=True); stream.write('\n'); temporary = Path(stream.name)
        temporary.replace(output)
        return 0
    except (LockError, EvidenceError, OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        print('Release lock rejected: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
