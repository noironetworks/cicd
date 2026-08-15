#!/usr/bin/env python3
"""Merge verified GitHub Actions results into the legacy test release stream."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Dict, List, Sequence, Tuple


TARGET_RELEASE = "6.1.1.7"
TARGET_STREAM = f"{TARGET_RELEASE}.z"
UPSTREAM_ID = "81c2369"
TARGET_Z_TAG = f"{TARGET_RELEASE}.{UPSTREAM_ID}.z"
QUAY_REGISTRY_PREFIX = "quay.io/noiro"
DOCKER_REGISTRY_PREFIX = "docker.io/noiro"
ACI_IMAGES: Tuple[str, ...] = (
    "aci-containers-host",
    "aci-containers-controller",
    "cnideploy",
    "aci-containers-operator",
    "openvswitch",
    "aci-containers-webhook",
    "aci-containers-certmanager",
    "aci-containers-host-ovscni",
)


@dataclass(frozen=True)
class Component:
    repository: str
    trigger_tag: str
    images: Tuple[str, ...]


COMPONENTS = {
    "aci-containers": Component(
        "noironetworks/aci-containers", TARGET_RELEASE, ACI_IMAGES
    ),
    "opflex": Component(
        "noironetworks/opflex", TARGET_RELEASE, ("opflex",)
    ),
}

DATED_TAG_RE = re.compile(
    rf"^{re.escape(TARGET_RELEASE)}\.{UPSTREAM_ID}\.([0-9]{{6}})\.([1-9][0-9]*)$"
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")
TOP_LEVEL_RELEASE_START = "- release_streams:"
RELEASE_TAG_PREFIX = "  release_tag:"
RELEASE_NAME_PREFIX = "    release_name:"
CONTAINER_IMAGES_LINE = "    container_images:"
IMAGE_ENTRY_PREFIX = "    - base-image:"
IMAGE_NAME_PREFIX = "      name:"
LAST_UPDATED_PREFIX = "    last_updated:"
MAX_RELEASES_FILE_BYTES = 20 * 1024 * 1024
SEVERITY_LABELS = ("Critical", "High", "Medium", "Low", "Unknown")
SEVERITY_TOKEN_MAP = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "unknown": "Unknown",
    "negligible": "Unknown",
}


class UpdateError(ValueError):
    """Raised when an input violates a publication invariant."""


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def parse_yaml_string(line: str, prefix: str, label: str) -> str:
    if not line.startswith(prefix):
        raise UpdateError(f"missing {label}")
    scalar = line[len(prefix) :].strip()
    if not scalar:
        raise UpdateError(f"empty {label}")
    if scalar.startswith('"'):
        try:
            value = json.loads(scalar)
        except json.JSONDecodeError as exc:
            raise UpdateError(f"invalid quoted {label}: {scalar}") from exc
        if not isinstance(value, str):
            raise UpdateError(f"quoted {label} must be a string")
        return value
    if any(character.isspace() for character in scalar) or "#" in scalar:
        raise UpdateError(f"unsupported {label} scalar: {scalar}")
    return scalar


def validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise UpdateError("timestamp must use UTC format YYYY-MM-DDTHH:MM:SSZ") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise UpdateError("timestamp is not canonical UTC")


def release_artifact_relpath(image_name: str, suffix: str) -> str:
    return f"release_artifacts/{TARGET_RELEASE}/z/{image_name}/{TARGET_RELEASE}-{suffix}.txt"


def read_optional_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("sha256:"):
        return text.removeprefix("sha256:")
    return text


def count_severity_from_report(path: Path) -> List[Dict[str, int]]:
    if path.is_symlink() or not path.is_file():
        raise UpdateError(f"missing scan report: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise UpdateError(f"cannot read scan report {path}: {exc}") from exc

    counts = {label: 0 for label in SEVERITY_LABELS}
    for line in lines[1:]:
        columns = line.split()
        if not columns:
            continue
        severity = ""
        for token in columns:
            mapped = SEVERITY_TOKEN_MAP.get(token.lower())
            if mapped is not None:
                severity = mapped
        if severity:
            counts[severity] += 1

    return [
        {
            "C": counts["Critical"],
            "H": counts["High"],
            "M": counts["Medium"],
            "L": counts["Low"],
            "U": counts["Unknown"],
        }
    ]


def validate_build_identity(
    image_z_tag: str,
    image_dated_tag: str,
    source_commit: str,
    cicd_commit: str,
    run_id: str,
    run_attempt: str,
    run_number: str,
) -> None:
    if image_z_tag != TARGET_Z_TAG:
        raise UpdateError(f"unexpected legacy z tag: {image_z_tag}")
    if not COMMIT_RE.fullmatch(source_commit):
        raise UpdateError("source commit must be a lowercase 40-character Git SHA")
    if not COMMIT_RE.fullmatch(cicd_commit):
        raise UpdateError("CICD commit must be a lowercase 40-character Git SHA")
    for label, value in (
        ("run ID", run_id),
        ("run attempt", run_attempt),
        ("run number", run_number),
    ):
        if not POSITIVE_INTEGER_RE.fullmatch(value):
            raise UpdateError(f"{label} must be a positive decimal integer")
    match = DATED_TAG_RE.fullmatch(image_dated_tag)
    if match is None:
        raise UpdateError(f"unexpected legacy dated tag: {image_dated_tag}")
    date_tag, tag_run_number = match.groups()
    try:
        parsed_date = datetime.strptime(date_tag, "%m%d%y")
    except ValueError as exc:
        raise UpdateError("dated image tag contains an invalid MMDDYY date") from exc
    if parsed_date.strftime("%m%d%y") != date_tag:
        raise UpdateError("dated image tag contains a non-canonical MMDDYY date")
    if tag_run_number != run_number:
        raise UpdateError("dated image tag does not match GITHUB_RUN_NUMBER")


def read_publication_manifest(
    path: Path,
    registry_prefix: str,
    expected_images: Sequence[str],
    image_z_tag: str,
    image_dated_tag: str,
) -> Dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise UpdateError(f"publication manifest is not a regular file: {path}")
    try:
        with path.open("r", encoding="utf-8", newline="") as manifest_file:
            rows = list(csv.reader(manifest_file, delimiter="\t", strict=True))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise UpdateError(f"cannot read publication manifest: {exc}") from exc
    if rows[:1] != [["image", "digest"]]:
        raise UpdateError("publication manifest header must be exactly: image<TAB>digest")
    if len(rows) != len(expected_images) * 2 + 1:
        raise UpdateError(
            f"publication manifest must contain exactly two tags for each of "
            f"{len(expected_images)} images"
        )

    expected_tags = {image_z_tag, image_dated_tag}
    publications: Dict[str, Dict[str, str]] = {}
    for line_number, row in enumerate(rows[1:], start=2):
        if len(row) != 2:
            raise UpdateError(f"manifest line {line_number} must have exactly two fields")
        image_ref, digest = row
        expected_prefix = f"{registry_prefix}/"
        if not image_ref.startswith(expected_prefix):
            raise UpdateError(f"unexpected registry in manifest line {line_number}")
        name_and_tag = image_ref[len(expected_prefix) :]
        if name_and_tag.count(":") != 1:
            raise UpdateError(f"invalid image reference in manifest line {line_number}")
        image_name, published_tag = name_and_tag.split(":", 1)
        if image_name not in expected_images:
            raise UpdateError(f"unexpected image in manifest line {line_number}: {image_name}")
        if published_tag not in expected_tags:
            raise UpdateError(f"unexpected tag for {image_name}: {published_tag}")
        if image_ref != f"{registry_prefix}/{image_name}:{published_tag}":
            raise UpdateError(f"non-canonical image reference at line {line_number}")
        if not DIGEST_RE.fullmatch(digest):
            raise UpdateError(f"invalid digest for {image_name}: {digest}")
        image_publications = publications.setdefault(image_name, {})
        if published_tag in image_publications:
            raise UpdateError(
                f"tag appears more than once for {image_name}: {published_tag}"
            )
        image_publications[published_tag] = digest

    result: Dict[str, str] = {}
    for image_name in expected_images:
        image_publications = publications.get(image_name, {})
        if set(image_publications) != expected_tags:
            raise UpdateError(f"publication manifest is missing a tag for {image_name}")
        if len(set(image_publications.values())) != 1:
            raise UpdateError(f"z and dated tags have different digests for {image_name}")
        result[image_name] = image_publications[image_z_tag]
    return result


def find_release_blocks(lines: Sequence[str]) -> List[Tuple[int, int, str]]:
    if next((line for line in lines if line.strip()), None) != "releases:":
        raise UpdateError("releases.yaml must begin with the top-level 'releases:' key")
    starts = [index for index, line in enumerate(lines) if line == TOP_LEVEL_RELEASE_START]
    if not starts:
        raise UpdateError("releases.yaml contains no recognizable release blocks")
    blocks: List[Tuple[int, int, str]] = []
    seen_tags = set()
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        tags = [
            parse_yaml_string(line, RELEASE_TAG_PREFIX, "top-level release_tag")
            for line in lines[start:end]
            if line.startswith(RELEASE_TAG_PREFIX)
        ]
        if len(tags) != 1:
            raise UpdateError(
                f"release block beginning on line {start + 1} must contain one release_tag"
            )
        if tags[0] in seen_tags:
            raise UpdateError(f"duplicate top-level release_tag: {tags[0]}")
        seen_tags.add(tags[0])
        blocks.append((start, end, tags[0]))
    return blocks


def find_release_stream(block: Sequence[str]) -> Tuple[int, int]:
    starts = [index for index, line in enumerate(block) if line.startswith("  - ")]
    matches: List[Tuple[int, int]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(block)
        names = [
            parse_yaml_string(line, RELEASE_NAME_PREFIX, "release_name")
            for line in block[start:end]
            if line.startswith(RELEASE_NAME_PREFIX)
        ]
        if len(names) != 1:
            raise UpdateError("each release stream must contain exactly one release_name")
        if names[0] == TARGET_STREAM:
            matches.append((start, end))
    if len(matches) != 1:
        raise UpdateError(f"expected exactly one release stream named {TARGET_STREAM}")
    return matches[0]


def find_image_entries(
    stream: Sequence[str],
) -> Tuple[int, int, List[Tuple[int, int, str]]]:
    container_indices = [i for i, line in enumerate(stream) if line == CONTAINER_IMAGES_LINE]
    metadata_indices = [i for i, line in enumerate(stream) if line.startswith(LAST_UPDATED_PREFIX)]
    if len(container_indices) != 1 or len(metadata_indices) != 1:
        raise UpdateError("target stream has invalid container_images or last_updated fields")
    container_index, metadata_index = container_indices[0], metadata_indices[0]
    if metadata_index <= container_index:
        raise UpdateError("target stream has invalid image boundaries")
    starts = [
        i
        for i, line in enumerate(stream)
        if container_index < i < metadata_index and line.startswith(IMAGE_ENTRY_PREFIX)
    ]
    entries: List[Tuple[int, int, str]] = []
    seen_names = set()
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else metadata_index
        names = [
            parse_yaml_string(line, IMAGE_NAME_PREFIX, "image name")
            for line in stream[start:end]
            if line.startswith(IMAGE_NAME_PREFIX)
        ]
        if len(names) != 1:
            raise UpdateError("each container image entry must contain exactly one name")
        if names[0] in seen_names:
            raise UpdateError(f"duplicate container image in target stream: {names[0]}")
        seen_names.add(names[0])
        entries.append((start, end, names[0]))
    return container_index, metadata_index, entries


def render_image_entries(
    component: Component,
    quay_digests: Dict[str, str],
    docker_digests: Dict[str, str],
    image_z_tag: str,
    image_dated_tag: str,
    source_commit: str,
    run_id: str,
    run_attempt: str,
    timestamp: str,
    docs_root: Path | None = None,
) -> Dict[str, List[str]]:
    run_url = f"https://github.com/{component.repository}/actions/runs/{run_id}/attempts/{run_attempt}"
    commit_url = f"https://github.com/{component.repository}/commit/{source_commit}"
    rendered: Dict[str, List[str]] = {}
    for image_name in component.images:
        quay_hex = quay_digests[image_name].removeprefix("sha256:")
        docker_hex = docker_digests[image_name].removeprefix("sha256:")
        cve_relpath = release_artifact_relpath(image_name, "cve")
        cve_base_relpath = release_artifact_relpath(image_name, "cve-base")
        sbom_relpath = release_artifact_relpath(image_name, "sbom")
        if docs_root is None:
            base_image_sha = ""
            severity = [{"C": 0, "H": 0, "M": 0, "L": 0, "U": 0}]
            base_severity = [{"C": 0, "H": 0, "M": 0, "L": 0, "U": 0}]
        else:
            base_image_sha = read_optional_text(
                docs_root
                / release_artifact_relpath(image_name, "base-image-sha")
            )
            severity = count_severity_from_report(docs_root / cve_relpath)
            base_severity = count_severity_from_report(docs_root / cve_base_relpath)
        rendered[image_name] = [
            "    - base-image:",
            f"      - cve: {yaml_string(cve_base_relpath)}",
            f"        severity: {json.dumps(base_severity, ensure_ascii=True)}",
            f"        severity_type: {yaml_string('quay')}",
            f"        sha: {yaml_string(base_image_sha)}",
            f"      build-logs: {yaml_string(run_url)}",
            f"      build-time: {yaml_string(timestamp)}",
            "      commit:",
            f"      - link: {yaml_string(commit_url)}",
            f"        sha: {yaml_string(source_commit)}",
            f"      cve: {yaml_string(cve_relpath)}",
            "      docker:",
            f"      - link: {yaml_string(f'https://hub.docker.com/layers/noiro/{image_name}/{image_z_tag}/images/sha256-{docker_hex}?context=explore')}",
            f"        sha: {yaml_string(docker_hex)}",
            f"        tag: {yaml_string(image_z_tag)}",
            f"      - link: {yaml_string(f'https://hub.docker.com/layers/noiro/{image_name}/{image_dated_tag}/images/sha256-{docker_hex}?context=explore')}",
            f"        sha: {yaml_string(docker_hex)}",
            f"        tag: {yaml_string(image_dated_tag)}",
            f"      name: {yaml_string(image_name)}",
            "      quay:",
            f"      - link: {yaml_string(f'https://quay.io/noiro/{image_name}:{image_z_tag}')}",
            f"        sha: {yaml_string(quay_hex)}",
            f"        tag: {yaml_string(image_z_tag)}",
            f"      - link: {yaml_string(f'https://quay.io/noiro/{image_name}:{image_dated_tag}')}",
            f"        sha: {yaml_string(quay_hex)}",
            f"        tag: {yaml_string(image_dated_tag)}",
            f"      sbom: {yaml_string(sbom_relpath)}",
            f"      severity: {json.dumps(severity, ensure_ascii=True)}",
            f"      severity_type: {yaml_string('quay')}",
        ]
    return rendered


def merge_release(
    path: Path,
    component: Component,
    incoming: Dict[str, List[str]],
    timestamp: str,
) -> bool:
    if path.is_symlink() or not path.is_file():
        raise UpdateError(f"releases file is not a regular file: {path}")
    if path.stat().st_size > MAX_RELEASES_FILE_BYTES:
        raise UpdateError("releases file exceeds the safe size limit")
    original = path.read_text(encoding="utf-8")
    if "\x00" in original:
        raise UpdateError("releases file contains a NUL byte")
    lines = original.splitlines()
    targets = [block for block in find_release_blocks(lines) if block[2] == TARGET_RELEASE]
    if len(targets) != 1:
        raise UpdateError(f"expected exactly one top-level release {TARGET_RELEASE}")

    block_start, block_end, _ = targets[0]
    block = list(lines[block_start:block_end])
    stream_start, stream_end = find_release_stream(block)
    stream = list(block[stream_start:stream_end])
    container_index, metadata_index, entries = find_image_entries(stream)
    existing_names = {name for _, _, name in entries}
    missing = set(component.images) - existing_names
    if missing:
        raise UpdateError(
            f"target stream is missing expected images: {', '.join(sorted(missing))}"
        )

    merged_entries: List[str] = []
    for start, end, image_name in entries:
        if image_name in incoming:
            merged_entries.extend(incoming[image_name])
        else:
            merged_entries.extend(stream[start:end])
    stream[container_index + 1 : metadata_index] = merged_entries
    last_updated_indices = [
        index for index, line in enumerate(stream) if line.startswith(LAST_UPDATED_PREFIX)
    ]
    if len(last_updated_indices) != 1:
        raise UpdateError("target stream has an invalid last_updated field")
    stream[last_updated_indices[0]] = f"    last_updated: {yaml_string(timestamp)}"
    block[stream_start:stream_end] = stream
    updated_lines = list(lines)
    updated_lines[block_start:block_end] = block

    updated = "\n".join(updated_lines) + "\n"
    if updated == original:
        return False
    current_mode = stat.S_IMODE(path.stat().st_mode)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(updated)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_name = temporary_file.name
        os.chmod(temporary_name, current_mode)
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return True


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", required=True, choices=tuple(COMPONENTS))
    parser.add_argument("--releases-file", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--docker-manifest", required=True, type=Path)
    parser.add_argument("--image-z-tag", required=True)
    parser.add_argument("--image-dated-tag", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--cicd-commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--run-number", required=True)
    parser.add_argument("--timestamp", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    component = COMPONENTS[args.component]
    try:
        validate_build_identity(
            args.image_z_tag,
            args.image_dated_tag,
            args.source_commit,
            args.cicd_commit,
            args.run_id,
            args.run_attempt,
            args.run_number,
        )
        validate_timestamp(args.timestamp)
        quay_digests = read_publication_manifest(
            args.manifest,
            QUAY_REGISTRY_PREFIX,
            component.images,
            args.image_z_tag,
            args.image_dated_tag,
        )
        docker_digests = read_publication_manifest(
            args.docker_manifest,
            DOCKER_REGISTRY_PREFIX,
            component.images,
            args.image_z_tag,
            args.image_dated_tag,
        )
        incoming = render_image_entries(
            component,
            quay_digests,
            docker_digests,
            args.image_z_tag,
            args.image_dated_tag,
            args.source_commit,
            args.run_id,
            args.run_attempt,
            args.timestamp,
            args.releases_file.parent.parent,
        )
        changed = merge_release(args.releases_file, component, incoming, args.timestamp)
    except (OSError, UnicodeError, UpdateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    action = "Updated" if changed else "Already current:"
    print(f"{action} {args.component} in test release stream {TARGET_STREAM}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
