#!/usr/bin/env python3
"""Resolve ACC publication intent without credentials or publication authority.

Explicit mode callers use the production policy before uploading to PyPI. Missing
mode describes the existing push-to-pypi.sh route, including its legacy selector;
that original entrypoint retains all its authorization and behavior.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


class ModeError(ValueError):
    pass


def resolve(mode, message, tag, build_number, legacy_selector='', actor='', triggering_actor=''):
    production = 'release to pypi.org' in message.lower()
    preview = 'release to test.pypi.org' in message.lower()
    if production == preview:
        raise ModeError('tag annotation must select exactly one PyPI registry')
    if mode is not None and mode not in ('preview', 'production'):
        raise ModeError('ACC_PUBLICATION_MODE must be preview or production')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?', tag):
        raise ModeError('invalid ACC package tag')
    legacy = mode is None
    if not legacy and legacy_selector:
        raise ModeError('explicit mode conflicts with the legacy TRAVIS_BUILD_USER selector')
    if not legacy and ((mode == 'preview') != preview):
        raise ModeError('explicit mode conflicts with the tag annotation')
    if not legacy and 'rc' in tag:
        raise ModeError('explicit GHA mode accepts numeric release tags only')
    development = preview and (not legacy or legacy_selector in ('noiro-tagger', 'noiro-generic'))
    if development and not re.fullmatch(r'[0-9]+', str(build_number)):
        raise ModeError('development package requires a numeric build number')
    version = f'{tag}.dev{build_number}' if development else tag
    return {
        'schema_version': 1, 'mode': mode if not legacy else 'legacy',
        'legacy_adapter': legacy, 'legacy_entrypoint': 'push-to-pypi.sh' if legacy else None,
        'requires_legacy_authorization': legacy,
        'index': 'testpypi' if preview else 'pypi', 'version': version,
        'development': development, 'trigger_tag': tag,
        'production_authorization_required': production,
        'provenance': {'actor': actor, 'triggering_actor': triggering_actor},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', default=os.environ.get('ACC_PUBLICATION_MODE'))
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--build-number', default=os.environ.get('GITHUB_RUN_NUMBER', os.environ.get('TRAVIS_BUILD_NUMBER', '')))
    parser.add_argument('--actor', default='')
    parser.add_argument('--triggering-actor', default='')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        tag_type = subprocess.check_output(['git', 'cat-file', '-t', f'refs/tags/{args.tag}'], cwd=args.repo, text=True).strip()
        if tag_type != 'tag':
            raise ModeError('publication tag must be annotated')
        message = subprocess.check_output(['git', 'for-each-ref', '--format=%(contents)', f'refs/tags/{args.tag}'], cwd=args.repo, text=True)
        result = resolve(args.mode, message, args.tag, args.build_number,
                         os.environ.get('TRAVIS_BUILD_USER', ''), args.actor, args.triggering_actor)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    except (ModeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'ACC publication mode rejected: {exc}\n')


if __name__ == '__main__':
    main()
