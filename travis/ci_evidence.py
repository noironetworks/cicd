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
from zoneinfo import ZoneInfo

import yaml

SCHEMA_VERSION = 1
TIMEOUT = 180
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
REPORTS = {"sbom", "cve", "cve-base", "buildlog"}


class EvidenceError(RuntimeError):
    pass


def clean_environment():
    # Preparation must never pass status/publishing secrets to external tools.
    return {k: v for k, v in os.environ.items()
            if not re.search(r"TOKEN|PASSWORD|PASSWD|PSWD|(?:^|_)PASS(?:$|_)|SECRET|TAGGER|ASKPASS", k, re.I)}


def run(command, *, cwd=None, env=None, timeout=TIMEOUT):
    try:
        return subprocess.run(command, cwd=cwd, env=env, check=True, text=True,
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
                         **identity(args), "records": [], "observations": [], "images": [], "files": []}

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


def resolve_digest(reference, observations, *, pull=True):
    env = clean_environment()
    if pull:
        run(["docker", "pull", reference], env=env)
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
            for key in ("tag", "sha", "base_image", "other_tags"):
                if not row.get(key):
                    raise EvidenceError(f"missing image metadata: {key}")
            if not DIGEST.fullmatch(row["sha"]):
                raise EvidenceError("image sha must be a Docker image ID")
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
            prefix = f"release_artifacts/{args.release_tag}/z/{image}/{args.release_tag}"
            grype = paths["cve"].read_text(encoding="utf-8")
            base_digest = resolve_digest(row["base_image"], builder.manifest["observations"], pull=False)
            registry_records = {"quay": [], "docker": []}
            for tag in (z_tag, dated_tag):
                quay = resolve_digest(f"{registry}/{image}:{tag}", builder.manifest["observations"])
                docker = resolve_digest(f"noiro/{image}:{tag}", builder.manifest["observations"])
                registry_records["quay"].append({"tag": tag, "sha": quay, "link": f"https://{registry}/{image}:{tag}"})
                registry_records["docker"].append({"tag": tag, "sha": docker,
                                                   "link": f"https://hub.docker.com/layers/noiro/{image}/{tag}/images/sha256-{docker}?context=explore"})
            # Actual Trivy execution occurs exactly once in token-free preparation.
            combined = builder.temp / f"{image}-cve.txt"
            combined.write_text(grype, encoding="utf-8")
            if args.source_repository.rsplit("/", 1)[-1] != "opflex":
                trivy = run(["trivy", "image", f"{registry}/{image}@sha256:{registry_records['quay'][0]['sha']}"], env=clean_environment(), timeout=900)
                with combined.open("a", encoding="utf-8") as stream:
                    stream.write(trivy)
            paths["cve"] = combined
            for key, path in paths.items():
                builder.add(path, f"docs/{prefix}-{key}.txt")
            record = {"name": image, "commit": commit_record(args), **registry_records,
                      "base-image": [{"sha": base_digest,
                                      "cve": prefix + "-cve-base.txt",
                                      "severity": severity(paths["cve-base"].read_text(encoding="utf-8"))}],
                      "sbom": prefix + "-sbom.txt", "cve": prefix + "-cve.txt",
                      "build-logs": prefix + "-buildlog.txt", "build-time": builder.manifest["prepared_at"],
                      "severity": severity(grype), "severity_type": "grype"}
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
        # Artifact text is UTF-8 in the status repository.
        target.write_text(run(["git", "show", f"{commits[0]}:{name}"], cwd=checkout,
                              env=clean_environment()), encoding="utf-8")
    return directory, directory


def prepare_package(args):
    valid_name(args.version, "package version")
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
        builder.add(artifact, f"docs/release_artifacts/{args.release_tag}/{directory}/acc-provision/{artifact.name}")
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
                    old_prefix = f"release_artifacts/{args.release_tag}/z/{name}/"
                    new_prefix = f"release_artifacts/{args.release_tag}/{directory}/{name}/"
                    for report in ("sbom", "cve", "cve-base", "buildlog"):
                        filename = f"{args.release_tag}-{report}.txt"
                        builder.add(local_file(origin, "docs/" + old_prefix + filename), "docs/" + new_prefix + filename)
                    record = {k: v for k, v in image.items() if not k.startswith("_")}
                    record.update({"quay": [{"tag": tag, "sha": quay, "link": f"https://quay.io/noiro/{name}:{tag}"}],
                                   "docker": [{"tag": tag, "sha": docker, "link": f"https://hub.docker.com/layers/noiro/{name}/{tag}/images/sha256-{docker}?context=explore"}],
                                   "sbom": new_prefix + args.release_tag + "-sbom.txt",
                                   "cve": new_prefix + args.release_tag + "-cve.txt",
                                   "build-logs": new_prefix + args.release_tag + "-buildlog.txt",
                                   "build-time": builder.manifest["prepared_at"]})
                    for base in record.get("base-image", []):
                        base["cve"] = new_prefix + args.release_tag + "-cve-base.txt"
                        base["severity"] = severity(local_file(origin, "docs/" + old_prefix + args.release_tag + "-cve-base.txt").read_text())
                    original_path = origin / "docs" / old_prefix / (args.release_tag + "-cve-base-original.txt")
                    if not original_path.exists():
                        original_path = local_file(origin, "docs/" + old_prefix + args.release_tag + "-cve-base.txt")
                    builder.add(original_path, "docs/" + new_prefix + args.release_tag + "-cve-base-original.txt")
                    record["base-image-original"] = [{"sha": record["base-image"][0]["sha"],
                                                        "cve": new_prefix + args.release_tag + "-cve-base-original.txt",
                                                        "severity": severity(original_path.read_text()), "severity_type": "grype",
                                                        "date-and-time": builder.manifest["prepared_at"]}]
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
    for record in manifest["records"]:
        name = valid_name(record["name"], "image name")
        if name in names:
            raise EvidenceError("duplicate image record")
        names.add(name)
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
    if manifest["kind"] == "images" and not names:
        raise EvidenceError("empty image evidence")
    if manifest["kind"] == "package":
        for package in manifest["package"]:
            if "docs/" + package["build-logs"] not in files:
                raise EvidenceError("package references absent evidence")
    for observation in manifest["observations"]:
        if not DIGEST.fullmatch(observation["digest"]):
            raise EvidenceError("invalid observed digest")
    for image in manifest.get("images", []):
        valid_name(image["name"], "image name")
        if not DIGEST.fullmatch(image["image_id"]):
            raise EvidenceError("invalid prepared image ID")
    return manifest, files


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
    stream["last_updated"] = manifest["prepared_at"]
    for record in manifest["records"]:
        old = next((item for item in stream["container_images"] if item.get("name") == record["name"]), None)
        if old is None:
            stream["container_images"].append(copy.deepcopy(record))
        else:
            # Preserve extension fields from concurrent components and maintainers.
            old.update(copy.deepcopy(record))
    if manifest["kind"] == "package":
        stream["acc_provision"] = copy.deepcopy(manifest["package"])
        if manifest.get("released"):
            stream["released"] = True
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
            updated = merge_status(copy.deepcopy(previous), manifest)
            if updated != previous:
                status.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
            for destination, source in files.items():
                target = safe_destination(checkout, destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            run(git + ["add", "--", "docs/release_artifacts"], cwd=checkout, env=env)
            changed = run(git + ["diff", "--cached", "--name-only"], cwd=checkout, env=env)
            if not changed.strip():
                print("CICD status already contains this evidence")
                return
            message = f"{manifest['stream']}-{manifest['source']['repository'].split('/')[-1]}-{manifest['run']['id']}"
            message += f"\n\nCommit: {manifest['source']['sha']}"
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
