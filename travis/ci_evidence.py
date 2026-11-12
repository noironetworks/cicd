#!/usr/bin/env python3
"""Prepare immutable release evidence without a status credential; publish data only.

Image metadata is JSONL, relative to its own directory, with registry, image, tag,
other_tags (list or CSV), sha, base_image, reports {sbom,cve,cve-base,buildlog}.
Optional image_z_tag and dated_tag override the legacy tag-derived defaults.
Preparation requires preinstalled Docker and Trivy; scan failures remain fatal.
Package promotion uses a read-only complete cicd-status checkout for historical
artifacts. No Docker, scanner, installer or HTTP command runs during publication.
"""

import argparse
import copy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from scan_result import observation

SCHEMA_VERSION = 1
TIMEOUT = 180
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
REPORTS = {"sbom", "cve", "cve-base", "buildlog", "scan", "trivy", "grype-image", "grype-base", "build-input", "build-input-provenance"}


class EvidenceError(RuntimeError):
    pass


def clean_environment():
    # Preparation must never pass status/publishing secrets to external tools.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")
            and not re.search(r"TOKEN|PASSWORD|PASSWD|PSWD|(?:^|_)PASS(?:$|_)|SECRET|TAGGER|ASKPASS", k, re.I)}


def run(command, *, cwd=None, env=None, timeout=TIMEOUT, binary=False):
    try:
        return subprocess.run(command, cwd=cwd, env=env, check=True, text=not binary,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout).stdout
    except subprocess.TimeoutExpired as exc:
        raise EvidenceError(f"{command[0]} operation timed out") from exc
    except subprocess.CalledProcessError as exc:
        # Child output can contain credentials supplied by external programs.
        raise EvidenceError(f"{command[0]} operation failed (exit {exc.returncode})") from exc


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise EvidenceError("invalid relative artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise EvidenceError("artifact paths must be relative without traversal")
    return path


def local_file(root, relative):
    relative_path(relative)
    root = Path(root).resolve()
    path = root / relative
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink():
            raise EvidenceError("symlink artifacts are not permitted")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise EvidenceError(f"missing artifact: {relative}")
    return path


def valid_name(value, description):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise EvidenceError(f"invalid {description}")
    return value


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now():
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")


def severity(text):
    values = []
    started = False
    for line in text.splitlines():
        if not started:
            started = "SEVERITY" in line
            continue
        if not line.strip():
            break
        matches = re.findall(r"\b(Critical|High|Medium|Low|Unknown|Negligible)\b", line)
        if not matches:
            break
        values.append("Unknown" if matches[-1] == "Negligible" else matches[-1])
    return [{key: values.count(value) for key, value in
             zip("CHMLU", ("Critical", "High", "Medium", "Low", "Unknown"))}]


def legacy_counts(text):
    if "No vulnerabilities found" in text:
        return dict.fromkeys("CHMLU", 0)
    lines = text.splitlines()
    index = next((index for index, line in enumerate(lines) if "SEVERITY" in line), None)
    if index is None:
        return None
    rows = []
    for line in lines[index + 1:]:
        if not line.strip():
            break
        if not re.search(r"\b(Critical|High|Medium|Low|Unknown|Negligible)\b", line):
            return None
        rows.append(line)
    return severity(text)[0] if rows else None


def validate_observation(value):
    if not isinstance(value, dict) or value.get("state") not in ("ok", "unavailable", "queued", "unsupported", "error"):
        raise EvidenceError("invalid scan observation state")
    if value.get("digest") and not DIGEST.fullmatch(value["digest"]):
        raise EvidenceError("invalid scan target digest")
    counts = value.get("counts")
    if value["state"] == "ok":
        if not isinstance(counts, dict) or set(counts) != set("CHMLU") or any(type(number) is not int or number < 0 for number in counts.values()):
            raise EvidenceError("successful scan requires complete counts")
    elif counts is not None:
        raise EvidenceError("failed scan may not contain current counts")


def report_counts(text):
    """Read known report formats without turning unavailable data into zero."""
    counts = legacy_counts(text)
    if counts is not None:
        return counts
    try:
        payload = json.loads(text)
        validate_observation(payload)
        return payload["counts"] if payload["state"] == "ok" else None
    except (ValueError, KeyError, TypeError, EvidenceError):
        return None


def identity(args):
    for key in ("release_tag", "trigger_tag", "upstream_id", "run_id"):
        valid_name(getattr(args, key), key)
    if not isinstance(args.source_repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.source_repository):
        raise EvidenceError("invalid source repository")
    if not args.source_sha or not SHA.fullmatch(args.source_sha):
        raise EvidenceError("source SHA must be a complete Git commit ID")
    number = args.run_number or args.run_id
    if not re.fullmatch(r"[1-9][0-9]*", number) or not re.fullmatch(r"[1-9][0-9]*", args.run_attempt):
        raise EvidenceError("run number and attempt must be positive integers")
    cicd_sha = run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                   env=clean_environment()).strip()
    return {"source": {"repository": args.source_repository, "sha": args.source_sha},
            "cicd": {"sha": cicd_sha},
            "run": {"id": args.run_id, "number": number, "attempt": args.run_attempt, "url": args.run_url,
                    "trigger_tag": args.trigger_tag, "upstream_id": args.upstream_id}}


class Builder:
    def __init__(self, args, kind, stream, directory):
        self.target = Path(args.bundle)
        if self.target.exists():
            raise EvidenceError("bundle destination already exists")
        self.temp = Path(tempfile.mkdtemp(prefix="ci-evidence-"))
        self.manifest = {"schema_version": SCHEMA_VERSION, "kind": kind,
                         "release_tag": args.release_tag, "stream": stream,
                         "directory": directory, "prepared_at": now(),
                         **identity(args), "records": [], "failed_records": [], "observations": [], "images": [], "files": [], "retained_files": []}

    def retain(self, source):
        name = valid_name(Path(source).name, "retained artifact filename")
        if Path(source).is_symlink() or not Path(source).is_file():
            raise EvidenceError("retained artifact must be a regular file")
        destination = self.temp / "retained" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        self.manifest["retained_files"].append({"path": "retained/" + name, "sha256": digest_file(destination),
                                                 "size": destination.stat().st_size})

    def add(self, source, destination):
        relative_path(destination)
        if any(item["path"] == destination for item in self.manifest["files"]):
            raise EvidenceError("duplicate artifact destination")
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise EvidenceError("artifact source must be a regular file")
        target = self.temp / "artifacts" / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        self.manifest["files"].append({"path": destination, "size": target.stat().st_size,
                                       "sha256": digest_file(target)})

    def finish(self):
        (self.temp / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        validate_bundle(self.temp)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        # copy then rename on the destination filesystem, including /tmp -> workspace.
        staging = Path(tempfile.mkdtemp(prefix=".ci-evidence-", dir=self.target.parent))
        try:
            shutil.copytree(self.temp, staging, dirs_exist_ok=True)
            staging.rename(self.target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(self.temp, ignore_errors=True)


def resolve_digest(reference, observations, *, pull=True, expected_image_id=None):
    env = clean_environment()
    if pull:
        run(["docker", "pull", reference], env=env)
    if expected_image_id:
        observed_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", reference], env=env).strip()
        if observed_id != expected_image_id:
            raise EvidenceError("published tag resolves to a different built image ID")
    raw = run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", reference], env=env)
    try:
        candidates = json.loads(raw)
    except ValueError as exc:
        raise EvidenceError("Docker returned invalid repository digests") from exc
    repository = reference.split("@", 1)[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    repository = repository.removeprefix("docker.io/")
    for item in candidates or []:
        name, separator, digest = item.partition("@")
        if separator and name.removeprefix("docker.io/") == repository and DIGEST.fullmatch(digest):
            observations.append({"reference": reference, "digest": digest})
            return digest.removeprefix("sha256:")
    raise EvidenceError(f"no repository digest for {reference}")


def commit_record(args):
    return [{"link": f"https://github.com/{args.source_repository}/commit/{args.source_sha}",
             "sha": args.source_sha}]


def generated_log(args, directory):
    path = directory / "buildlog.txt"
    path.write_text(f"GitHub Actions run: {args.run_url}\nSource commit: {args.source_sha}\n"
                    f"Trigger tag: {args.trigger_tag}\n", encoding="utf-8")
    return path


def prepare_images(args):
    metadata = Path(args.metadata)
    builder = Builder(args, "images", args.release_tag + ".z", "z")
    seen = set()
    try:
        rows = [json.loads(line) for line in metadata.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows:
            raise EvidenceError("metadata contains no images")
        for row in rows:
            image = valid_name(row["image"], "image name")
            if image in seen:
                raise EvidenceError("duplicate image metadata")
            seen.add(image)
            registry = row["registry"]
            if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_./-]+", registry):
                raise EvidenceError("invalid image registry")
            for key in ("tag", "base_image", "other_tags"):
                if not row.get(key):
                    raise EvidenceError(f"missing image metadata: {key}")
            if not row.get("publication") and not DIGEST.fullmatch(row.get("sha") or ""):
                raise EvidenceError("image sha must be a Docker image ID")
            if DIGEST.fullmatch(row.get("sha") or ""):
                builder.manifest["images"].append({"name": image, "image_id": row["sha"]})
            tags = row["other_tags"]
            if isinstance(tags, str):
                tags = tags.split(",")
            z_tag = valid_name(row.get("image_z_tag") or os.environ.get("IMAGE_Z_TAG") or
                               f"{args.release_tag}.{args.upstream_id}.z", "image z tag")
            dated_tag = row.get("dated_tag") or os.environ.get("GHA_DATED_IMAGE_TAG") or tags[-1]
            valid_name(dated_tag, "dated tag")
            if dated_tag == z_tag:
                raise EvidenceError("dated tag and z tag must differ")
            reports = row["reports"]
            if not isinstance(reports, dict) or not {"sbom", "cve", "cve-base"}.issubset(reports):
                raise EvidenceError("missing per-image reports")
            if set(reports) - REPORTS:
                raise EvidenceError("unknown report kind")
            paths = {key: local_file(metadata.parent, value) for key, value in reports.items()}
            if "buildlog" not in paths:
                paths["buildlog"] = generated_log(args, builder.temp)
            scan_policy = row.get("scan_policy", "fatal")
            failed_default = scan_policy == "fatal" and row.get("publication", {}).get("state") in ("failed", "partial")
            prefix = f"release_artifacts/{args.release_tag}/z/{image}/"
            if failed_default:
                prefix += f"failures/{args.run_id}-{args.run_attempt}/"
            prefix += args.release_tag
            grype = paths["cve"].read_text(encoding="utf-8")
            scan_data = copy.deepcopy(row.get("scan") or {})
            base_digest = scan_data.get("base", {}).get("base_repository_digest", "").removeprefix("sha256:")
            registry_records = {"quay": [], "docker": []}
            if row.get("publication"):
                # Receipts describe only verified attempts; never read an old
                # alias after a partial push and report it as this build.
                for item in row["publication"].get("references", []):
                    if not DIGEST.fullmatch(item.get("digest", "")):
                        raise EvidenceError("invalid publication receipt digest")
                    builder.manifest["observations"].append({"reference": item["reference"], "digest": item["digest"]})
                    key = item["registry"]
                    if key in registry_records:
                        digest = item["digest"].removeprefix("sha256:")
                        link = (f"https://hub.docker.com/layers/noiro/{image}/{item['tag']}/images/sha256-{digest}?context=explore"
                                if key == "docker" else "https://" + item["reference"])
                        registry_records[key].append({"tag": item["tag"], "sha": digest, "link": link})
                for values in registry_records.values():
                    values.sort(key=lambda value: (value["tag"] != z_tag, value["tag"]))
            else:
                base_digest = resolve_digest(row["base_image"], builder.manifest["observations"], pull=False)
                for tag in (z_tag, dated_tag):
                    quay = resolve_digest(f"{registry}/{image}:{tag}", builder.manifest["observations"], expected_image_id=row["sha"])
                    docker = resolve_digest(f"noiro/{image}:{tag}", builder.manifest["observations"], expected_image_id=row["sha"])
                    registry_records["quay"].append({"tag": tag, "sha": quay, "link": f"https://{registry}/{image}:{tag}"})
                    registry_records["docker"].append({"tag": tag, "sha": docker,
                                                       "link": f"https://hub.docker.com/layers/noiro/{image}/{tag}/images/sha256-{docker}?context=explore"})
            if not scan_data:
                # Older retained text reports are visible, with no invented
                # freshness or successful empty scan when input is absent.
                counts = legacy_counts(grype)
                scan_data["image"] = observation("ok" if counts is not None else "error", digest=row.get("sha"), scanner="grype-legacy",
                                                 report=prefix + "-cve.txt", counts=counts)
                base_text = paths["cve-base"].read_text(encoding="utf-8")
                base_counts = legacy_counts(base_text)
                scan_data["base"] = observation("ok" if base_counts is not None else "error", digest="sha256:" + base_digest,
                                                scanner="grype-legacy", report=prefix + "-cve-base.txt",
                                                counts=base_counts)
                scan_data["trivy"] = observation("queued", reason="not-collected-by-legacy-build")
            for target in ("image", "base"):
                validate_observation(scan_data[target])
            if scan_data["image"].get("digest") and scan_data["image"]["digest"] != row.get("sha"):
                raise EvidenceError("scan observation does not match built image ID")
            combined = builder.temp / f"{image}-cve.txt"
            combined.write_text(grype, encoding="utf-8")
            paths["cve"] = combined
            for key, path in paths.items():
                builder.add(path, f"docs/{prefix}-{key}.txt")
            base_record = {"cve": prefix + "-cve-base.txt"}
            if base_digest:
                base_record["sha"] = base_digest
            if scan_data["base"].get("state") == "ok":
                base_record["severity"] = [scan_data["base"]["counts"]]
            for target, suffix in (("image", "cve"), ("base", "cve-base")):
                immutable = (f"release_artifacts/{args.release_tag}/z/{image}/scans/"
                             f"{args.run_id}-{args.run_attempt}/{suffix}.txt")
                builder.add(paths[suffix], "docs/" + immutable)
                scan_data[target]["report"] = immutable
                if scan_data[target].get("last_success"):
                    scan_data[target]["last_success"]["report"] = scan_data[target]["report"]
            record = {"name": image, "commit": commit_record(args), **registry_records,
                      "base-image": [base_record],
                      "sbom": prefix + "-sbom.txt", "cve": prefix + "-cve.txt",
                      "build-logs": prefix + "-buildlog.txt", "build-time": builder.manifest["prepared_at"],
                      "severity_type": "grype"}
            if scan_policy not in ("fatal", "advisory"):
                raise EvidenceError("unknown scan policy in metadata")
            if scan_policy == "advisory":
                record["scan"] = {"build": {key: scan_data[key] for key in ("image", "base")}}
                record["scan-tools"] = {key: value for key, value in scan_data.items() if key not in ("image", "base")}
            if scan_data["image"].get("state") == "ok":
                record["severity"] = [scan_data["image"]["counts"]]
            if row.get("publication"):
                record["publication"] = copy.deepcopy(row["publication"])
                publication_file = builder.temp / (image + "-publication.json")
                publication_file.write_text(json.dumps(row["publication"], sort_keys=True) + "\n")
                builder.add(publication_file, "docs/" + prefix + "-publication.json")
            for build_input in row.get("build_inputs", []):
                if (build_input.get("captured_before_build") is not True or not DIGEST.fullmatch(build_input.get("digest", ""))
                        or not DIGEST.fullmatch(build_input.get("image_id", ""))):
                    raise EvidenceError("invalid bound build input proof")
                if build_input not in builder.manifest.setdefault("build_inputs", []):
                    builder.manifest["build_inputs"].append(copy.deepcopy(build_input))
            if failed_default:
                builder.manifest["failed_records"].append(record)
                builder.manifest["images"] = [item for item in builder.manifest["images"] if item["name"] != image]
            else:
                builder.manifest["records"].append(record)
        builder.finish()
    finally:
        shutil.rmtree(builder.temp, ignore_errors=True)


def historical_artifacts(checkout, image, release, digest):
    """Select the existing z evidence or the historical commit matching image ID."""
    current = image["quay"][0]["sha"]
    if current == digest:
        return checkout, None
    ref = f"quay.io/noiro/{image['name']}:{image['_promotion_tag']}"
    image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], env=clean_environment()).strip()
    if not DIGEST.fullmatch(image_id):
        raise EvidenceError("invalid promoted image ID")
    commits = run(["git", "log", "--fixed-strings", "--grep=" + image_id, "--format=%H"],
                  cwd=checkout, env=clean_environment()).splitlines()
    if not commits:
        raise EvidenceError("no historical evidence for promoted image ID")
    directory = Path(tempfile.mkdtemp(prefix="ci-history-"))
    prefix = f"docs/release_artifacts/{release}/z/{image['name']}"
    names = run(["git", "ls-tree", "-r", "--name-only", commits[0], "--", prefix],
                cwd=checkout, env=clean_environment()).splitlines()
    if not names:
        raise EvidenceError("historical image artifacts are missing")
    for name in names:
        relative_path(name)
        mode = run(["git", "ls-tree", commits[0], "--", name], cwd=checkout,
                   env=clean_environment()).split()[0]
        if mode != "100644" and mode != "100755":
            raise EvidenceError("historical symlink artifacts are not permitted")
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(run(["git", "show", f"{commits[0]}:{name}"], cwd=checkout,
                               env=clean_environment(), binary=True))
    historical_status = directory / "docs/release_artifacts/releases.yaml"
    historical_status.parent.mkdir(parents=True, exist_ok=True)
    historical_status.write_text(run(["git", "show", commits[0] + ":docs/release_artifacts/releases.yaml"],
                                     cwd=checkout, env=clean_environment()), encoding="utf-8")
    return directory, directory


def prepare_package(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", args.version):
        raise EvidenceError("invalid package version")
    directory, stream = "z", args.release_tag + ".z"
    if args.mode == "release":
        directory, stream = "r", args.release_tag
    elif args.mode == "rc":
        if not args.rc_number or not re.fullmatch(r"[1-9][0-9]*", args.rc_number):
            raise EvidenceError("--rc-number must be positive for RC preparation")
        directory, stream = "rc" + args.rc_number, args.release_tag + ".rc" + args.rc_number
    builder = Builder(args, "package", stream, directory)
    try:
        prefix = f"release_artifacts/{args.release_tag}/{directory}/acc-provision/{args.release_tag}"
        artifact = Path(args.artifact)
        valid_name(artifact.name, "package filename")
        registry = urlsplit(args.registry)
        index = {"pypi.org": "pypi", "test.pypi.org": "testpypi"}.get(registry.hostname)
        if registry.scheme != "https" or registry.username or registry.password or not index:
            raise EvidenceError("package status must reference the selected PyPI index")
        validate_package_link(args.registry, args.version, index)
        builder.manifest["package_artifact"] = {"name": "acc-provision", "version": args.version,
                                                 "index": index, "filename": artifact.name,
                                                 "sha256": digest_file(artifact), "size": artifact.stat().st_size}
        builder.retain(artifact)
        log = Path(args.build_log) if args.build_log else generated_log(args, builder.temp)
        builder.add(log, "docs/" + prefix + "-buildlog.txt")
        builder.manifest["package"] = [{"link": args.registry, "tag": args.version,
                                        "commit": commit_record(args), "build-logs": prefix + "-buildlog.txt",
                                        "build-time": builder.manifest["prepared_at"]}]
        builder.manifest["released"] = args.mode == "release"
        if args.mode != "z" and "post" not in args.version:
            if not args.status_checkout:
                raise EvidenceError("release/RC promotion requires --status-checkout")
            checkout = Path(args.status_checkout).resolve()
            status = yaml.safe_load(local_file(checkout, "docs/release_artifacts/releases.yaml").read_text())
            release = next(item for item in status["releases"] if item["release_tag"] == args.release_tag)
            z = next(item for item in release["release_streams"] if item["release_name"] == args.release_tag + ".z")
            tag = f"{args.release_tag}.{args.upstream_id}"
            if args.mode == "rc":
                tag += ".rc" + args.rc_number
            for original in z["container_images"]:
                image = copy.deepcopy(original)
                name = valid_name(image["name"], "image name")
                quay = resolve_digest(f"quay.io/noiro/{name}:{tag}", builder.manifest["observations"])
                docker = resolve_digest(f"noiro/{name}:{tag}", builder.manifest["observations"])
                image["_promotion_tag"] = tag
                # Historical selection also checks the Docker digest, as legacy did.
                check_digest = quay if docker == original["docker"][0]["sha"] else "mismatch"
                origin, cleanup = historical_artifacts(checkout, image, args.release_tag, check_digest)
                try:
                    if cleanup:
                        historical = yaml.safe_load(local_file(origin, "docs/release_artifacts/releases.yaml").read_text())
                        historical_release = next(item for item in historical["releases"] if item["release_tag"] == args.release_tag)
                        historical_stream = next(item for item in historical_release["release_streams"] if item["release_name"] == args.release_tag + ".z")
                        image = copy.deepcopy(next(item for item in historical_stream["container_images"] if item["name"] == name))
                    old_prefix = f"release_artifacts/{args.release_tag}/z/{name}/"
                    new_prefix = f"release_artifacts/{args.release_tag}/{directory}/{name}/"
                    # Preserve every historical byte, including Quay JSON,
                    # provenance SHA files and CR/non-UTF-8 Travis logs.
                    artifact_root = origin / "docs" / old_prefix
                    for source in sorted(artifact_root.rglob("*")):
                        if source.is_symlink():
                            raise EvidenceError("symlink release artifact is prohibited")
                        if source.is_file():
                            builder.add(source, "docs/" + new_prefix + source.relative_to(artifact_root).as_posix())
                    record = {k: v for k, v in image.items() if not k.startswith("_")}
                    # Refresh observations belong to the independently scheduled
                    # target stream; promotion carries build evidence only.
                    if isinstance(record.get("scan"), dict):
                        record["scan"].pop("refresh", None)
                    record.update({"quay": [{"tag": tag, "sha": quay, "link": f"https://quay.io/noiro/{name}:{tag}"}],
                                   "docker": [{"tag": tag, "sha": docker, "link": f"https://hub.docker.com/layers/noiro/{name}/{tag}/images/sha256-{docker}?context=explore"}],
                                   "sbom": new_prefix + args.release_tag + "-sbom.txt",
                                   "cve": new_prefix + args.release_tag + "-cve.txt",
                                   "build-logs": new_prefix + args.release_tag + "-buildlog.txt",
                                   "build-time": builder.manifest["prepared_at"]})
                    build_scan = record.get("scan", {}).get("build", {}).get("image", {})
                    if build_scan:
                        if build_scan.get("state") == "ok":
                            record["severity"] = [build_scan["counts"]]
                        else:
                            record.pop("severity", None)
                    else:
                        counts = report_counts(local_file(origin, "docs/" + old_prefix + args.release_tag + "-cve.txt").read_text(errors="replace"))
                        if counts is None:
                            record.pop("severity", None)
                        else:
                            record["severity"] = [counts]
                    record["severity_type"] = "grype"
                    for field in ("cve_error", "severity_link"):
                        record.pop(field, None)
                    for base in record.get("base-image", []):
                        if base.get("cve", "").startswith(old_prefix):
                            base["cve"] = new_prefix + base["cve"][len(old_prefix):]
                    original_path = origin / "docs" / old_prefix / (args.release_tag + "-cve-base-original.txt")
                    if not original_path.exists():
                        original_path = local_file(origin, "docs/" + old_prefix + args.release_tag + "-cve-base.txt")
                    original_destination = "docs/" + new_prefix + args.release_tag + "-cve-base-original.txt"
                    if not any(item["path"] == original_destination for item in builder.manifest["files"]):
                        builder.add(original_path, original_destination)
                    original_text = original_path.read_text(errors="replace")
                    original_counts = report_counts(original_text)
                    original_base = {"cve": new_prefix + args.release_tag + "-cve-base-original.txt",
                                     "severity_type": "grype" if original_counts is not None else "unknown", "date-and-time": builder.manifest["prepared_at"]}
                    if record["base-image"][0].get("sha"):
                        original_base["sha"] = record["base-image"][0]["sha"]
                    if original_counts is not None:
                        original_base["severity"] = [original_counts]
                    record["base-image-original"] = [original_base]
                    for namespace in record.get("scan", {}).values():
                        for target in namespace.values():
                            for field in (target, target.get("last_success", {})):
                                if field.get("report", "").startswith(old_prefix):
                                    original_report = field["report"]
                                    promoted_report = new_prefix + original_report[len(old_prefix):]
                                    if not any(item["path"] == "docs/" + promoted_report for item in builder.manifest["files"]):
                                        builder.add(local_file(origin, "docs/" + original_report), "docs/" + promoted_report)
                                    field["report"] = promoted_report
                    sha_path = origin / "docs" / old_prefix / (args.release_tag + "-base-image-sha.txt")
                    if sha_path.exists():
                        saved_sha = local_file(origin, "docs/" + old_prefix + args.release_tag + "-base-image-sha.txt").read_text().strip().removeprefix("sha256:")
                        if re.fullmatch(r"[0-9a-f]{64}", saved_sha):
                            record["base-image-original"][0]["sha"] = saved_sha
                    record["update-release-cves-until"] = (datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(days=730)).strftime("%Y-%m-%d %H:%M:%S %Z")
                    builder.manifest["records"].append(record)
                finally:
                    if cleanup:
                        shutil.rmtree(cleanup)
        builder.finish()
    finally:
        shutil.rmtree(builder.temp, ignore_errors=True)


def validate_bundle(root):
    root = Path(root)
    if root.is_symlink():
        raise EvidenceError("bundle root may not be a symlink")
    manifest = json.loads(local_file(root, "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") not in ("images", "package"):
        raise EvidenceError("unsupported bundle schema")
    release = valid_name(manifest["release_tag"], "release tag")
    directory = manifest["directory"]
    if directory not in ("z", "r") and not re.fullmatch(r"rc[1-9][0-9]*", directory):
        raise EvidenceError("invalid release directory")
    expected_stream = release + ".z" if directory == "z" else release if directory == "r" else release + "." + directory
    if manifest["stream"] != expected_stream:
        raise EvidenceError("stream does not match artifact directory")
    if not SHA.fullmatch(manifest["source"]["sha"]) or not SHA.fullmatch(manifest["cicd"]["sha"]):
        raise EvidenceError("invalid bundle identity")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", manifest["source"]["repository"]):
        raise EvidenceError("invalid bundle source repository")
    for key in ("id", "number", "attempt"):
        if not isinstance(manifest["run"].get(key), str) or not re.fullmatch(r"[1-9][0-9]*", manifest["run"][key]):
            raise EvidenceError("invalid bundle run identity")
    for key in ("trigger_tag", "upstream_id"):
        valid_name(manifest["run"][key], "run " + key)
    if not isinstance(manifest["prepared_at"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (?:PST|PDT)", manifest["prepared_at"]):
        raise EvidenceError("invalid prepared timestamp")
    if manifest["kind"] == "images" and (directory != "z" or "released" in manifest):
        raise EvidenceError("image bundles may update only the z stream")
    if manifest["kind"] == "package" and manifest.get("images"):
        raise EvidenceError("package bundles may not forge image build history")
    if manifest.get("released", False) and (manifest["kind"] != "package" or directory != "r"):
        raise EvidenceError("only release package bundles may mark a stream released")
    prefix = f"docs/release_artifacts/{release}/{directory}/"
    files = {}
    for item in manifest["files"]:
        destination = str(relative_path(item["path"]))
        if not destination.startswith(prefix) or destination in files:
            raise EvidenceError("artifact outside release directory or duplicated")
        path = local_file(root, "artifacts/" + destination)
        if path.stat().st_size != item["size"] or digest_file(path) != item["sha256"]:
            raise EvidenceError("artifact size/hash mismatch")
        files[destination] = path
    names = set()
    for record in manifest["records"] + manifest.get("failed_records", []):
        name = valid_name(record["name"], "image name")
        if name in names:
            raise EvidenceError("duplicate image record")
        names.add(name)
        if manifest["kind"] == "images" and record.get("commit") != [{
                "sha": manifest["source"]["sha"],
                "link": f"https://github.com/{manifest['source']['repository']}/commit/{manifest['source']['sha']}"}]:
            raise EvidenceError("image record source differs from bundle source")
        for key in ("sbom", "cve", "build-logs"):
            if "docs/" + record[key] not in files:
                raise EvidenceError("record references absent evidence")
        for base_key in ("base-image", "base-image-original"):
            for base in record.get(base_key, []):
                if "docs/" + base["cve"] not in files:
                    raise EvidenceError("base record references absent evidence")
        for registry in ("quay", "docker"):
            for image in record[registry]:
                valid_name(image["tag"], "image tag")
                if not re.fullmatch(r"[0-9a-f]{64}", image["sha"]):
                    raise EvidenceError("invalid image digest")
        for value in record.get("scan", {}).get("build", {}).values():
            validate_observation(value)
            for evidence in (value, value.get("last_success", {})):
                if evidence.get("report") and "docs/" + str(relative_path(evidence["report"])) not in files:
                    raise EvidenceError("scan observation references absent evidence")
    if manifest["kind"] == "images" and not names:
        raise EvidenceError("empty image evidence")
    if manifest["kind"] == "package":
        expected = manifest["package_artifact"]
        if expected.get("name") != "acc-provision" or expected.get("index") not in ("pypi", "testpypi"):
            raise EvidenceError("invalid package artifact identity")
        artifact_path = "retained/" + valid_name(expected["filename"], "package filename")
        artifact = local_file(root, artifact_path)
        if digest_file(artifact) != expected["sha256"] or artifact.stat().st_size != expected["size"]:
            raise EvidenceError("retained package differs from prepared identity")
        for package in manifest["package"]:
            if package.get("commit") != [{"sha": manifest["source"]["sha"],
                                          "link": f"https://github.com/{manifest['source']['repository']}/commit/{manifest['source']['sha']}"}]:
                raise EvidenceError("package record source differs from bundle source")
            if package["tag"] != expected["version"]:
                raise EvidenceError("package record version mismatch")
            validate_package_link(package["link"], expected["version"], expected["index"])
            if "docs/" + package["build-logs"] not in files:
                raise EvidenceError("package references absent evidence")
    for observation in manifest["observations"]:
        if not DIGEST.fullmatch(observation["digest"]):
            raise EvidenceError("invalid observed digest")
    descriptor_names = set()
    for image in manifest.get("images", []):
        valid_name(image["name"], "image name")
        if image["name"] in descriptor_names:
            raise EvidenceError("duplicate image build history")
        descriptor_names.add(image["name"])
        if not DIGEST.fullmatch(image["image_id"]):
            raise EvidenceError("invalid prepared image ID")
    if manifest["kind"] == "images" and {item["name"] for item in manifest["images"]} != {record["name"] for record in manifest["records"]}:
        raise EvidenceError("image build history does not match image records")
    retained = manifest.get("retained_files", [])
    if manifest["kind"] == "images" and retained:
        raise EvidenceError("image bundles may not contain retained package artifacts")
    if manifest["kind"] == "package" and len(retained) != 1:
        raise EvidenceError("package bundle must retain exactly one package artifact")
    for item in retained:
        parts = relative_path(item["path"]).parts
        if len(parts) != 2 or parts[0] != "retained":
            raise EvidenceError("invalid retained artifact path")
        path = local_file(root, item["path"])
        if digest_file(path) != item["sha256"] or path.stat().st_size != item["size"]:
            raise EvidenceError("retained artifact size/hash mismatch")
    return manifest, files


def validate_expected_scope(args, manifest, files):
    expected = {"release_tag": args.expected_release_tag, "kind": args.expected_kind,
                "stream": args.expected_stream, "source.repository": args.expected_source_repository,
                "source.sha": args.expected_source_sha, "cicd.sha": args.expected_cicd_sha,
                "run.id": args.expected_run_id}
    for key, value in expected.items():
        current = manifest
        for part in key.split("."):
            current = current[part]
        if current != value:
            raise EvidenceError("bundle differs from trusted publisher scope: " + key)
    if args.expected_run_attempt and manifest["run"]["attempt"] != args.expected_run_attempt:
        raise EvidenceError("bundle differs from expected producer attempt")
    allowed = {valid_name(name, "expected image") for name in args.expected_images.split(",") if name}
    names = {record["name"] for record in manifest["records"] + manifest.get("failed_records", [])}
    if not names.issubset(allowed):
        raise EvidenceError("bundle contains another component's images")
    if manifest["kind"] == "package" and manifest["directory"] != "z" and (not allowed or names != allowed):
        raise EvidenceError("release package requires the complete promoted image cohort")
    owners = names | ({"acc-provision"} if manifest["kind"] == "package" else set())
    for path in files:
        if PurePosixPath(path).parts[4] not in owners:
            raise EvidenceError("bundle writes another component's artifact directory")


def replay_guard(checkout, manifest):
    path = safe_destination(checkout, f"docs/release_artifacts/{manifest['release_tag']}/{manifest['directory']}/.ci-evidence.json")
    index = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "components": {}}
    if index.get("schema_version") != 1 or not isinstance(index.get("components"), dict):
        raise EvidenceError("invalid publication identity index")
    names = [record["name"] for record in manifest["records"]]
    if manifest["kind"] == "package":
        names.append("acc-provision")
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    order = (int(manifest["run"]["id"]), int(manifest["run"]["attempt"]))
    keys = [manifest["kind"] + ":" + manifest["source"]["repository"] + ":" + name for name in names]
    already = bool(keys)
    for key in keys:
        prior = index["components"].get(key)
        if not prior or tuple(prior["order"]) != order:
            already = False
        if prior and tuple(prior["order"]) > order:
            return path, index, True
        if prior and tuple(prior["order"]) == order and prior["bundle_sha256"] != fingerprint:
            raise EvidenceError("producer run/attempt already has a different frozen bundle")
    if already:
        return path, index, True
    for key in keys:
        index["components"][key] = {"order": list(order), "bundle_sha256": fingerprint,
                                     "source_sha": manifest["source"]["sha"], "cicd_sha": manifest["cicd"]["sha"]}
    return path, index, False


def merge_status(data, manifest):
    data = data or {}
    if not isinstance(data, dict) or not isinstance(data.setdefault("releases", []), list):
        raise EvidenceError("invalid status release schema")
    release = next((item for item in data["releases"] if item.get("release_tag") == manifest["release_tag"]), None)
    if release is None:
        release = {"release_tag": manifest["release_tag"], "release_streams": []}
        data["releases"].append(release)
    for name in (manifest["release_tag"] + ".z", manifest["release_tag"], manifest["stream"]):
        if not any(item.get("release_name") == name for item in release["release_streams"]):
            stream = {"release_name": name, "last_updated": manifest["prepared_at"],
                      "container_images": [], "acc_provision": []}
            if name == manifest["release_tag"]:
                stream["released"] = False
            release["release_streams"].append(stream)
    stream = next(item for item in release["release_streams"] if item["release_name"] == manifest["stream"])
    before = copy.deepcopy(stream)
    before.pop("last_updated", None)
    for record in manifest["records"]:
        old = next((item for item in stream["container_images"] if item.get("name") == record["name"]), None)
        if old is None:
            stream["container_images"].append(copy.deepcopy(record))
        else:
            # Preserve extension fields from concurrent components and maintainers.
            incoming = copy.deepcopy(record)
            if "scan" in incoming:
                scans = copy.deepcopy(old.get("scan", {}))
                for namespace, targets in incoming["scan"].items():
                    prior = scans.get(namespace, {})
                    for target, value in targets.items():
                        previous_observation = prior.get(target, {})
                        if value.get("state") != "ok":
                            last_success = previous_observation.get("last_success")
                            if last_success:
                                value["last_success"] = copy.deepcopy(last_success)
                    scans[namespace] = targets
                incoming["scan"] = scans
            if incoming.get("scan", {}).get("build", {}).get("image", {}).get("state") != "ok":
                old.pop("severity", None)
            for field in ("cve_error", "severity_link"):
                old.pop(field, None)
            old.update(incoming)
    if manifest["kind"] == "package":
        if manifest["records"]:
            # A release/RC promotion selects a complete z cohort. Components
            # removed from that cohort must not survive from an older release.
            names = [record["name"] for record in manifest["records"]]
            stream["container_images"] = [next(item for item in stream["container_images"] if item["name"] == name)
                                           for name in names]
        stream["acc_provision"] = copy.deepcopy(manifest["package"])
        if manifest.get("released"):
            stream["released"] = True
    after = copy.deepcopy(stream)
    after.pop("last_updated", None)
    if after != before:
        stream["last_updated"] = max(stream.get("last_updated", ""), manifest["prepared_at"])
    return data


def safe_destination(checkout, relative):
    relative_path(relative)
    target = checkout / relative
    current = checkout
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink():
            raise EvidenceError("status checkout contains symlink destination")
    if not target.resolve().is_relative_to(checkout.resolve()):
        raise EvidenceError("destination escapes checkout")
    return target


def publish(args):
    # Full validation precedes even clone; failed bundles cannot write Git state.
    manifest, files = validate_bundle(args.bundle)
    validate_expected_scope(args, manifest, files)
    if manifest["kind"] == "package":
        validate_package_receipt(manifest, args.receipt)
    repository = args.repository
    if repository.startswith("https://"):
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", repository):
            raise EvidenceError("status remote must be an approved GitHub HTTPS repository")
    elif not Path(repository).is_absolute() or not Path(repository).is_dir():
        raise EvidenceError("status remote must use HTTPS or an explicit local bare repository")
    elif run(["git", "--git-dir", repository, "rev-parse", "--is-bare-repository"], env=clean_environment()).strip() != "true":
        raise EvidenceError("local status remote must be bare")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", args.branch) or ".." in args.branch:
        raise EvidenceError("invalid status branch")
    with tempfile.TemporaryDirectory(prefix="ci-status-") as temporary:
        temporary = Path(temporary)
        # Freeze the verified bytes in private storage. A caller may replace its
        # downloaded artifact directory while Git retries are still in progress.
        frozen = temporary / "bundle"
        frozen.mkdir()
        (frozen / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        for destination, source in files.items():
            target = frozen / "artifacts" / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        for item in manifest.get("retained_files", []):
            target = frozen / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_file(args.bundle, item["path"]), target)
        manifest, files = validate_bundle(frozen)
        helper = temporary / "askpass.sh"
        helper.write_text('#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" x-access-token;;\n'
                          '*Password*) printf "%s\\n" "${TRAVIS_TAGGER:?}";;\n*) exit 1;;\nesac\n')
        helper.chmod(0o700)
        env = {key: value for key, value in clean_environment().items() if not key.startswith("GIT_")}
        if os.environ.get("TRAVIS_TAGGER"):
            env["TRAVIS_TAGGER"] = os.environ["TRAVIS_TAGGER"]
        env.update(GIT_ASKPASS=str(helper), GIT_TERMINAL_PROMPT="0",
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ALLOW_PROTOCOL="https:file")
        # Use no inherited credential helpers and no ambient extra HTTP headers.
        git = ["git", "-c", "credential.helper=", "-c", "http.extraHeader=", "-c", "core.hooksPath=" + os.devnull]
        for attempt in range(1, 4):
            checkout = temporary / f"attempt-{attempt}"
            run(git + ["clone", "--quiet", "--single-branch", "--branch", args.branch,
                       "--", repository, str(checkout)], env=env)
            original = run(git + ["rev-parse", "HEAD"], cwd=checkout, env=env).strip()
            status = safe_destination(checkout, "docs/release_artifacts/releases.yaml")
            status.parent.mkdir(parents=True, exist_ok=True)
            previous = yaml.safe_load(status.read_text(encoding="utf-8")) if status.exists() else None
            identity_path, identity_index, stale = replay_guard(checkout, manifest)
            if stale:
                print("A newer producer run already published these components")
                return
            updated = merge_status(copy.deepcopy(previous), manifest)
            if updated != previous:
                status.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
            for destination, source in files.items():
                target = safe_destination(checkout, destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                if "/scans/" in destination and target.exists() and digest_file(target) != digest_file(source):
                    raise EvidenceError("a run's immutable scan evidence already exists with different bytes")
                shutil.copyfile(source, target)
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            identity_path.write_text(json.dumps(identity_index, sort_keys=True, indent=2) + "\n")
            run(git + ["add", "--", "docs/release_artifacts"], cwd=checkout, env=env)
            changed = run(git + ["diff", "--cached", "--name-only"], cwd=checkout, env=env)
            if not changed.strip():
                print("CICD status already contains this evidence")
                return
            message = f"{manifest['stream']}-{manifest['source']['repository'].split('/')[-1]}-{manifest['run']['id']}"
            message += f"\n\nCommit: {manifest['source']['sha']}"
            message += f"\nCICD: {manifest['cicd']['sha']}"
            message += f"\nRun: https://github.com/{manifest['source']['repository']}/actions/runs/{manifest['run']['id']}"
            for image in manifest.get("images", []):
                # Match legacy ImageId history lookup on package promotion.
                message += "\nImage: " + image["name"] + "\nImageId: " + image["image_id"]
            run(git + ["-c", "user.name=travis-tagger", "-c", "user.email=test@cisco.com",
                       "commit", "--quiet", "-m", message], cwd=checkout, env=env)
            try:
                run(git + ["push", "origin", f"HEAD:refs/heads/{args.branch}"], cwd=checkout, env=env)
                print("Published CICD status evidence")
                return
            except EvidenceError:
                remote = run(git + ["ls-remote", "origin", "refs/heads/" + args.branch], cwd=checkout, env=env).split()
                if not remote or remote[0] == original or attempt == 3:
                    raise
                print(f"CICD status advanced concurrently; reapplying evidence ({attempt}/3)", file=sys.stderr)
    raise EvidenceError("CICD status publication exhausted retries")


def validate_package_receipt(manifest, path):
    if not path:
        raise EvidenceError("package status publication requires --receipt")
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise EvidenceError("upload receipt must be a regular file")
    receipt = json.loads(source.read_text(encoding="utf-8"))
    if receipt.get("schema_version") != 1 or receipt.get("verified") is not True:
        raise EvidenceError("upload receipt must prove verified artifact identity")
    expected = manifest["package_artifact"]
    if not isinstance(receipt.get("package"), dict):
        raise EvidenceError("upload receipt is missing package identity")
    for key in ("name", "version", "index", "filename", "sha256", "size"):
        if receipt["package"].get(key) != expected[key]:
            raise EvidenceError("upload receipt package identity mismatch: " + key)
    root = "https://pypi.org" if expected["index"] == "pypi" else "https://test.pypi.org"
    if receipt.get("index") != root:
        raise EvidenceError("upload receipt index mismatch")
    if receipt.get("source_sha") != manifest["source"]["sha"] or receipt.get("cicd_commit") != manifest["cicd"]["sha"]:
        raise EvidenceError("upload receipt source/CICD identity mismatch")
    if expected["index"] == "pypi":
        if receipt.get("tag") != manifest["run"]["trigger_tag"] or not SHA.fullmatch(receipt.get("tag_object", "")):
            raise EvidenceError("production receipt tag identity mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", receipt.get("input_lock_sha256", "")):
            raise EvidenceError("production receipt is missing approved input lock identity")


def validate_package_link(value, version, index):
    host = "pypi.org" if index == "pypi" else "test.pypi.org"
    expected = f"https://{host}/project/acc-provision/{version}/#files"
    if value != expected:
        raise EvidenceError("package index link is not the canonical name/version link")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("prepare-images", "prepare-package"):
        child = sub.add_parser(command)
        child.add_argument("--bundle", required=True)
        child.add_argument("--run-number", default=os.environ.get("GITHUB_RUN_NUMBER") or os.environ.get("TRAVIS_BUILD_NUMBER"))
        child.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
        for flag, env in (("release-tag", "RELEASE_TAG"), ("source-repository", "TRAVIS_REPO_SLUG"),
                          ("source-sha", "TRAVIS_COMMIT"), ("run-id", "TRAVIS_BUILD_NUMBER"),
                          ("run-url", "TRAVIS_JOB_WEB_URL"), ("trigger-tag", "TRAVIS_TAG"),
                          ("upstream-id", "UPSTREAM_ID")):
            child.add_argument("--" + flag, default=os.environ.get(env), required=not bool(os.environ.get(env)))
        if command == "prepare-images":
            child.add_argument("--metadata", required=True)
            child.set_defaults(func=prepare_images)
        else:
            child.add_argument("--artifact", required=True)
            child.add_argument("--mode", choices=("z", "release", "rc"), required=True)
            child.add_argument("--version", required=True)
            child.add_argument("--registry", required=True)
            child.add_argument("--rc-number")
            child.add_argument("--status-checkout")
            child.add_argument("--build-log")
            child.set_defaults(func=prepare_package)
    child = sub.add_parser("publish")
    child.add_argument("--bundle", required=True)
    child.add_argument("--repository", required=True)
    child.add_argument("--branch", required=True)
    child.add_argument("--receipt", help="verified upload receipt, required for package bundles")
    for flag in ("release-tag", "kind", "stream", "source-repository", "source-sha", "cicd-sha", "run-id", "images"):
        child.add_argument("--expected-" + flag, required=True)
    child.add_argument("--expected-run-attempt")
    child.set_defaults(func=publish)
    return root


def main():
    try:
        args = parser().parse_args()
        args.func(args)
        return 0
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, StopIteration, yaml.YAMLError) as exc:
        print(f"Evidence error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
