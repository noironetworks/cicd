#!/usr/bin/env python3
"""Release authorization and immutable package recovery; never builds a package.

The signed tag remains the production approval. Network collection is separate
from upload, and subprocesses receive only the credential they actually need.
"""

from __future__ import annotations

import argparse
import copy
import email
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml


class PolicyError(RuntimeError):
    """A release cannot be authorized or safely reconciled."""


SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
TAG = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
RUNTIME_IMAGES = (
    "aci-containers-host", "aci-containers-controller", "cnideploy",
    "aci-containers-operator", "openvswitch", "aci-containers-webhook",
    "aci-containers-certmanager", "aci-containers-host-ovscni", "opflex",
)
INDEXES = {
    "pypi": ("https://pypi.org", "https://upload.pypi.org/legacy/"),
    "testpypi": ("https://test.pypi.org", "https://test.pypi.org/legacy/"),
}
SECRET_NAMES = {
    "TRAVIS_TAGGER", "CICD_STATUS_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
    "PYPI_USER", "PYPI_PASS", "TEST_PYPI_USER", "TEST_PYPI_PASS",
    "TWINE_USERNAME", "TWINE_PASSWORD", "TWINE_TOKEN",
}


def clean_environment():
    return {key: value for key, value in os.environ.items()
            if key not in SECRET_NAMES and not re.search(
                r"TOKEN|PASSWORD|PASSWD|PSWD|SECRET|TAGGER|ASKPASS|(?:^|_)PASS$", key, re.I)
            and not key.startswith("GIT_CONFIG_")}


def command(argv, *, cwd=None, env=None, input_text=None, timeout=60):
    try:
        result = subprocess.run(argv, cwd=cwd, env=clean_environment() if env is None else env,
                                input=input_text, text=True, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PolicyError(f"{Path(argv[0]).name} operation failed or timed out") from error
    if result.returncode:
        # Tool stderr may include remote URLs, credentials or signer identities.
        raise PolicyError(f"{Path(argv[0]).name} operation failed ({result.returncode})")
    return result.stdout


def git(repo, *args, env=None):
    environment = clean_environment() if env is None else dict(env)
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0")
    return command(["git", "-C", str(repo), "-c", "credential.helper=",
                    "-c", "http.extraHeader=", *args], env=environment).strip()


def load_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise PolicyError("missing or malformed JSON evidence") from error


def write_json(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False,
                                     encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(target)


def file_hash(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise PolicyError("artifact must be a regular non-symlink file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def package_manifest(artifact, name, version, index, source_sha=None, cicd_commit=None):
    raw_path = Path(artifact)
    if raw_path.is_symlink():
        raise PolicyError("artifact symlink is not accepted")
    artifact = raw_path.resolve(strict=True)
    if index not in INDEXES or not version or not re.fullmatch(r"[A-Za-z0-9_.+-]+", version):
        raise PolicyError("invalid package target or version")
    if not SHA.fullmatch(str(source_sha or "")) or not SHA.fullmatch(str(cicd_commit or "")):
        raise PolicyError("package source and CICD commits must be explicit")
    digest = file_hash(artifact)
    try:
        with tarfile.open(artifact, "r:gz") as archive:
            members = [member for member in archive.getmembers()
                       if member.isfile() and member.name.count("/") == 1
                       and member.name.endswith("/PKG-INFO")]
            if len(members) != 1 or members[0].size > 1024 * 1024:
                raise PolicyError("expected one bounded top-level PKG-INFO")
            with archive.extractfile(members[0]) as stream:
                metadata = email.message_from_bytes(stream.read())
    except (OSError, tarfile.TarError) as error:
        raise PolicyError("invalid source distribution") from error
    if canonical_name(metadata.get("Name", "")) != canonical_name(name):
        raise PolicyError("distribution name does not match")
    if metadata.get("Version") != version:
        raise PolicyError("distribution version does not match")
    return {"schema_version": 1, "name": canonical_name(name), "version": version,
            "index": index, "filename": artifact.name, "sha256": digest,
            "artifact": str(artifact), "size": artifact.stat().st_size,
            "source_sha": source_sha, "cicd_commit": cicd_commit}


def manifest_identity(manifest):
    return {key: value for key, value in manifest.items() if key != "artifact"}


def validate_manifest(manifest, artifact=None):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise PolicyError("unsupported package manifest")
    path = Path(artifact or manifest.get("artifact", ""))
    if path.is_symlink():
        raise PolicyError("artifact symlink is not accepted")
    current = package_manifest(path, manifest.get("name", ""),
                               manifest.get("version", ""), manifest.get("index"),
                               manifest.get("source_sha"), manifest.get("cicd_commit"))
    for key in ("name", "version", "index", "filename", "sha256", "size", "source_sha", "cicd_commit"):
        if current[key] != manifest.get(key):
            raise PolicyError(f"package manifest {key} mismatch")
    return current


def tag_identity(repo, tag):
    if not TAG.fullmatch(tag):
        raise PolicyError("production tag must have form N.N.N.N")
    ref = "refs/tags/" + tag
    if git(repo, "cat-file", "-t", ref) != "tag":
        raise PolicyError("release tag must be annotated")
    obj = git(repo, "rev-parse", ref)
    source = git(repo, "rev-parse", ref + "^{commit}")
    if not SHA.fullmatch(obj) or not SHA.fullmatch(source):
        raise PolicyError("invalid release object identity")
    message = git(repo, "for-each-ref", "--format=%(contents)", ref)
    message = message.split("-----BEGIN PGP SIGNATURE-----", 1)[0].lower()
    if "release to pypi.org" not in message or "release to test.pypi.org" in message:
        raise PolicyError("tag must select production PyPI exclusively")
    return {"tag": tag, "tag_object": obj, "source_sha": source}


def verify_tag(repo, tag, policy, public_key):
    identity = tag_identity(repo, tag)
    fingerprint = str(policy.get("fingerprint", "")).upper()
    signer_email = str(policy.get("email", "")).casefold()
    if not re.fullmatch(r"[A-F0-9]{40}|[A-F0-9]{64}", fingerprint) or "@" not in signer_email:
        raise PolicyError("approved signing policy is missing")
    key = Path(public_key)
    if key.is_symlink() or not key.is_file() or key.stat().st_size > 1024 * 1024:
        raise PolicyError("approved public key file is missing or unsafe")
    data = key.read_text(encoding="utf-8")
    if "PRIVATE KEY" in data or "-----BEGIN PGP PUBLIC KEY BLOCK-----" not in data:
        raise PolicyError("only a public signing key is accepted")
    with tempfile.TemporaryDirectory(prefix="release-verification-") as directory:
        home = Path(directory)
        home.chmod(0o700)
        (home / "gpg.conf").write_text("no-auto-key-retrieve\nno-auto-key-import\n", encoding="utf-8")
        env = clean_environment()
        env["GNUPGHOME"] = directory
        command(["gpg", "--batch", "--homedir", directory, "--import", str(key.resolve())], env=env)
        listing = command(["gpg", "--batch", "--homedir", directory, "--with-colons",
                           "--list-keys", fingerprint], env=env)
        primary = None
        acceptable_email = False
        for line in listing.splitlines():
            fields = line.split(":")
            if fields[0] == "pub":
                if fields[1] in ("r", "e", "d", "i"):
                    raise PolicyError("authorized key is not currently valid")
                if fields[6] and int(fields[6]) <= time.time():
                    raise PolicyError("authorized key has expired")
            elif fields[0] == "fpr" and primary is None:
                primary = fields[9].upper()
            elif fields[0] == "uid" and fields[1] not in ("r", "e", "d", "i"):
                uid = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), fields[9])
                if email.utils.parseaddr(uid)[1].casefold() == signer_email:
                    acceptable_email = True
        if primary != fingerprint or not acceptable_email:
            raise PolicyError("public key does not match the authorized key and identity")
        result = subprocess.run(["git", "-C", str(repo), "-c", "gpg.format=openpgp",
                                 "-c", "gpg.program=gpg", "-c", "gpg.openpgp.program=gpg",
                                 "verify-tag", "--raw", "--", tag],
                                env=env, capture_output=True, text=True, timeout=60, check=False)
        statuses = [line.split() for line in result.stderr.splitlines()
                    if line.startswith("[GNUPG:] ")]
        forbidden = {"BADSIG", "ERRSIG", "NO_PUBKEY", "EXPSIG", "EXPKEYSIG", "REVKEYSIG",
                     "KEYEXPIRED", "SIGEXPIRED"}
        valid = [fields for fields in statuses if len(fields) > 2 and fields[1] == "VALIDSIG"]
        if (result.returncode or any(fields[1] in forbidden for fields in statuses)
                or len(valid) != 1 or fingerprint not in (valid[0][2].upper(), valid[0][-1].upper())):
            raise PolicyError("authorized tag signature verification failed")
    if tag_identity(repo, tag) != identity:
        raise PolicyError("tag changed during signature verification")
    return identity


def validate_input_lock(lock, release_tag, observations):
    if not isinstance(lock, dict) or lock.get("schema_version") != 1 or lock.get("release_tag") != release_tag:
        raise PolicyError("release input lock version/tag mismatch")
    if any(key in lock for key in ("source_sha", "tag_object", "acc_provision_commit")):
        raise PolicyError("input lock must not contain its enclosing source/tag identity")
    if not SHA.fullmatch(str(lock.get("cicd_commit", ""))):
        raise PolicyError("locked CICD commit is required")
    sources = lock.get("sources", {})
    if not isinstance(sources, dict) or any(not SHA.fullmatch(str(sources.get(repo, "")))
                                          for repo in ("noironetworks/opflex", "noironetworks/aci-containers")):
        raise PolicyError("locked product source commits are required")
    images = lock.get("images", {})
    if not isinstance(images, dict) or set(images) != set(RUNTIME_IMAGES):
        raise PolicyError("lock must contain the complete supported runtime image set")
    if not isinstance(observations, dict) or observations.get("schema_version") != 1:
        raise PolicyError("registry observations are missing")
    expected = {}
    for name in RUNTIME_IMAGES:
        entry = images[name]
        if not isinstance(entry, dict) or entry.get("platform") != "linux/amd64":
            raise PolicyError("image platform must be explicit")
        producer = entry.get("producer", {})
        source_repo = "noironetworks/opflex" if name == "opflex" else "noironetworks/aci-containers"
        if (producer.get("repository") != source_repo or producer.get("source_sha") != sources[source_repo]
                or producer.get("cicd_commit") != lock["cicd_commit"]
                or not str(producer.get("run_id", "")).isdigit()
                or int(producer.get("run_attempt", 0)) < 1):
            raise PolicyError("image producer identity does not match locked inputs")
        registries = entry.get("registries", {})
        if set(registries) != {"quay.io/noiro", "docker.io/noiro"}:
            raise PolicyError("both consumer registries must be locked")
        for registry, item in registries.items():
            ref, digest = item.get("reference", ""), item.get("digest", "")
            if not ref.startswith(registry + "/" + name + ":" ) or ref.endswith(".z") or not DIGEST.fullmatch(digest):
                raise PolicyError("consumer reference/digest is invalid")
            expected[ref] = digest
    dependencies = lock.get("dependencies", [])
    if not isinstance(dependencies, list) or not any("/opflex-build-base" in d.get("reference", "") for d in dependencies):
        raise PolicyError("OpFlex build-base dependency must be locked")
    for item in dependencies:
        if not DIGEST.fullmatch(item.get("digest", "")) or not item.get("reference"):
            raise PolicyError("invalid dependency identity")
        if item["reference"] in expected:
            raise PolicyError("a dependency duplicates a runtime or another dependency constraint")
        expected[item["reference"]] = item["digest"]
    actual = observations.get("digests", {})
    if any(actual.get(ref) != digest for ref, digest in expected.items()):
        raise PolicyError("required image/dependency is missing or has a different digest")
    if any(observations.get("platforms", {}).get(ref) != "linux/amd64" for ref in expected):
        raise PolicyError("required image/dependency platform has not been verified")
    return copy.deepcopy(lock)


def validate_checks(evidence, source_sha, requirements=None):
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or evidence.get("source_sha") != source_sha:
        raise PolicyError("trusted-test evidence source mismatch")
    requirements = requirements if requirements is not None else evidence.get("requirements", [])
    if not requirements:
        raise PolicyError("trusted test requirements are missing")
    for required in requirements:
        runs = [run for run in evidence.get("runs", [])
                if run.get("repository") == required.get("repository")
                and run.get("workflow") == required.get("workflow")
                and run.get("branch") == required.get("branch")
                and run.get("source_sha") == required.get("source_sha", source_sha) and run.get("event") == "push"]
        if not runs:
            raise PolicyError("required exact-source branch-push tests are missing")
        latest = max(runs, key=lambda run: (int(run.get("run_number", 0)), int(run.get("run_attempt", 0))))
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise PolicyError("latest trusted test attempt has not passed")
        jobs = latest.get("jobs", {})
        if not required.get("jobs") or any(jobs.get(name) != "success" for name in required["jobs"]):
            raise PolicyError("a required trusted test job did not succeed")
    return copy.deepcopy(evidence)


def http_json(url, *, token=None, allow_missing=False):
    headers = {"Accept": "application/json", "User-Agent": "noironetworks-release-policy"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
            raw = response.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise PolicyError("remote response is too large")
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code == 404 and allow_missing:
            return None
        raise PolicyError(f"remote evidence request failed ({error.code})") from error
    except (OSError, ValueError) as error:
        raise PolicyError("remote evidence request failed or was malformed") from error


def collect_checks(repository, source_sha, branch, workflow, jobs):
    if not re.fullmatch(r"noironetworks/[A-Za-z0-9_.-]+", repository) or not SHA.fullmatch(source_sha):
        raise PolicyError("invalid canonical test source")
    if "/" in workflow or not workflow.endswith((".yml", ".yaml")) or not branch:
        raise PolicyError("invalid trusted workflow selection")
    root = "https://api.github.com/repos/" + repository
    query = urllib.parse.urlencode({"event": "push", "head_sha": source_sha, "branch": branch, "per_page": 100})
    token = os.environ.get("GITHUB_TOKEN")
    payload = http_json(root + "/actions/workflows/" + urllib.parse.quote(workflow, safe="") + "/runs?" + query, token=token)
    runs = []
    for run in payload.get("workflow_runs", []):
        if (run.get("head_sha") != source_sha or run.get("event") != "push"
                or run.get("head_branch") != branch
                or run.get("head_repository", {}).get("full_name") != repository):
            continue
        attempt = int(run.get("run_attempt", 1))
        result = http_json(root + f"/actions/runs/{int(run['id'])}/attempts/{attempt}/jobs?per_page=100", token=token)
        if result.get("total_count", 0) > 100:
            raise PolicyError("required test run exceeds supported job budget")
        path = str(run.get("path", "")).split("@", 1)[0]
        if path != ".github/workflows/" + workflow:
            continue
        runs.append({"repository": repository, "workflow": workflow, "branch": branch,
                     "source_sha": source_sha, "event": "push", "id": run["id"],
                     "run_number": run["run_number"], "run_attempt": attempt,
                     "status": run["status"], "conclusion": run.get("conclusion"),
                     "jobs": {job["name"]: job.get("conclusion") for job in result.get("jobs", [])}})
    return {"schema_version": 1, "source_sha": source_sha,
            "requirements": [{"repository": repository, "workflow": workflow, "branch": branch, "jobs": jobs}],
            "runs": runs}


def check_requirements(policy, lock, source_sha):
    required = policy.get("required_tests")
    if not isinstance(required, list) or not required:
        raise PolicyError("trusted workflow policy is missing")
    branch = lock.get("source_branch")
    if branch not in policy.get("source_branches", []):
        raise PolicyError("source branch is not authorized by release policy")
    sources = {**lock.get("sources", {}), policy["repository"]: source_sha}
    result = []
    for item in required:
        repository = item.get("repository", policy["repository"])
        selected = sources.get(repository, "")
        if not SHA.fullmatch(str(selected)):
            raise PolicyError("required cohort source is missing from the signed lock")
        result.append({"repository": repository, "workflow": item["workflow"],
                       "branch": branch, "jobs": item["jobs"], "source_sha": selected})
    return result


def collect_cohort_checks(policy, lock, source_sha):
    requirements = check_requirements(policy, lock, source_sha)
    runs = []
    for required in requirements:
        evidence = collect_checks(required["repository"], required["source_sha"],
                                  required["branch"], required["workflow"], required["jobs"])
        runs.extend(evidence["runs"])
    return {"schema_version": 1, "source_sha": source_sha,
            "requirements": requirements, "runs": runs}


def inspect_inputs(lock):
    refs = [item["reference"] for entry in lock.get("images", {}).values()
            for item in entry.get("registries", {}).values()]
    refs += [item["reference"] for item in lock.get("dependencies", [])]
    digests = {}
    platforms = {}
    platform_digests = {}
    for reference in refs:
        if not re.fullmatch(r"(?:quay\.io|docker\.io)/[A-Za-z0-9_./:@-]+", reference):
            raise PolicyError("only approved public registry references may be observed")
        try:
            result = json.loads(command(["docker", "buildx", "imagetools", "inspect", "--format", "{{json .Manifest}}", reference]))
        except ValueError as error:
            raise PolicyError("malformed registry observation") from error
        digest = result.get("digest", "")
        if not DIGEST.fullmatch(digest):
            raise PolicyError("registry did not return an immutable manifest digest")
        selected = digest
        if "manifests" in result:
            candidates = [item for item in result["manifests"]
                          if item.get("platform", {}).get("os") == "linux"
                          and item.get("platform", {}).get("architecture") == "amd64"]
            if len(candidates) != 1 or not DIGEST.fullmatch(candidates[0].get("digest", "")):
                raise PolicyError("registry has no unambiguous linux/amd64 manifest")
            selected = candidates[0]["digest"]
        immutable = reference.split("@", 1)[0] + "@" + selected
        try:
            config = json.loads(command(["docker", "buildx", "imagetools", "inspect",
                                         "--format", "{{json .Image}}", immutable]))
        except ValueError as error:
            raise PolicyError("malformed platform image configuration") from error
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise PolicyError("registry image configuration has the wrong platform")
        digests[reference] = digest
        platforms[reference] = "linux/amd64"
        platform_digests[reference] = selected
    return {"schema_version": 1, "digests": digests, "platforms": platforms,
            "platform_digests": platform_digests}


def committed_lock(repo, source_sha, path):
    root = Path(repo).resolve()
    candidate = Path(path).resolve()
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError as error:
        raise PolicyError("release lock must be inside the signed source") from error
    original = git(root, "show", source_sha + ":" + relative)
    if load_json(candidate) != json.loads(original):
        raise PolicyError("working release lock differs from the signed source")
    return load_json(candidate)


def validate_package_pins(manifest, lock):
    """Check the actual retained distribution, not only mutable source YAML."""
    fields = {
        "aci-containers-host": "aci_containers_host_version",
        "aci-containers-controller": "aci_containers_controller_version",
        "cnideploy": "cnideploy_version", "aci-containers-operator": "aci_containers_operator_version",
        "openvswitch": "openvswitch_version", "aci-containers-webhook": "aci_containers_webhook_version",
        "aci-containers-certmanager": "aci_containers_certmanager_version",
        "aci-containers-host-ovscni": "aci_containers_host_ovscni_version", "opflex": "opflex_agent_version",
    }
    try:
        with tarfile.open(manifest["artifact"], "r:gz") as archive:
            entries = [item for item in archive.getmembers() if item.isfile()
                       and item.name.count("/") == 2 and item.name.endswith("/acc_provision/versions.yaml")]
            if len(entries) != 1 or entries[0].size > 1024 * 1024:
                raise PolicyError("retained package is missing one bounded versions.yaml")
            with archive.extractfile(entries[0]) as stream:
                versions = yaml.safe_load(stream.read()).get("versions", {})
    except (tarfile.TarError, OSError, yaml.YAMLError, AttributeError) as error:
        raise PolicyError("retained package image pins are malformed") from error
    series = ".".join(lock["release_tag"].split(".")[:2])
    rows = [row for key, row in versions.items() if str(key) == series]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise PolicyError("package has no unambiguous active release series")
    for image, field in fields.items():
        pin = str(rows[0].get(field, ""))
        if not pin or pin.endswith(".z"):
            raise PolicyError("production package must use locked consumer references")
        for registry, item in lock["images"][image]["registries"].items():
            if item["reference"] != registry + "/" + image + ":" + pin:
                raise PolicyError("retained package image pin does not match approved input lock")


def authorize_source(repo, tag, policy_path, public_key, lock_path, checks_path):
    """Verify the existing signed-tag approval before any consumer promotion.

    Registry readiness is checked separately to avoid requiring plain tags to
    exist before the authorized promotion which creates them.
    """
    policy = load_json(policy_path)
    identity = verify_tag(repo, tag, policy, public_key)
    lock = committed_lock(repo, identity["source_sha"], lock_path)
    if lock.get("release_tag") != tag or lock.get("schema_version") != 1:
        raise PolicyError("signed input lock does not match the approved release")
    helper_commit = git(Path(__file__).resolve().parent, "rev-parse", "HEAD")
    if lock.get("cicd_commit") != helper_commit:
        raise PolicyError("running CICD revision does not match the signed input lock")
    requirements = check_requirements(policy, lock, identity["source_sha"])
    validate_checks(load_json(checks_path), identity["source_sha"], requirements)
    if identity != tag_identity(repo, tag):
        raise PolicyError("release changed while authorizing")
    return identity, lock


def authorize(repo, tag, policy_path, public_key, lock_path, checks_path, observations_path, manifest_path):
    identity, lock = authorize_source(repo, tag, policy_path, public_key, lock_path, checks_path)
    manifest = validate_manifest(load_json(manifest_path))
    if manifest["index"] != "pypi" or manifest["version"] != tag or manifest["name"] != "acc-provision":
        raise PolicyError("production artifact must match signed release identity")
    validate_input_lock(lock, tag, load_json(observations_path))
    if manifest["cicd_commit"] != lock["cicd_commit"]:
        raise PolicyError("package CICD revision does not match the signed input lock")
    if manifest["source_sha"] != identity["source_sha"]:
        raise PolicyError("package source does not match the signed release")
    validate_package_pins(manifest, lock)
    return {"schema_version": 1, **identity, "package": manifest_identity(manifest),
            "public_key_sha256": file_hash(public_key), "policy_sha256": file_hash(policy_path),
            "input_lock_sha256": file_hash(lock_path), "cicd_commit": lock["cicd_commit"],
            "checks_sha256": file_hash(checks_path), "authorized_at": int(time.time())}


def assert_remote_tag(repo, identity):
    """Read only: the approved tag object and peeled source must still match."""
    current = tag_identity(repo, identity["tag"])
    if current != {key: identity[key] for key in ("tag", "tag_object", "source_sha")}:
        raise PolicyError("selected production tag/source changed")
    lines = git(repo, "ls-remote", "--exit-code", "origin",
                "refs/tags/" + identity["tag"], "refs/tags/" + identity["tag"] + "^{}")
    remote = dict((parts[1], parts[0]) for line in lines.splitlines() if len(parts := line.split()) == 2)
    if (remote.get("refs/tags/" + identity["tag"]) != identity["tag_object"]
            or remote.get("refs/tags/" + identity["tag"] + "^{}") != identity["source_sha"]):
        raise PolicyError("remote production tag/source changed")


def recheck(repo, authorization, manifest):
    if authorization.get("schema_version") != 1 or authorization.get("package") != manifest_identity(manifest):
        raise PolicyError("package does not match authorized artifact")
    if manifest["name"] != "acc-provision" or manifest["version"] != authorization.get("tag") or manifest["index"] != "pypi":
        raise PolicyError("production package must match the authorized release")
    assert_remote_tag(repo, authorization)


def existing_package(manifest):
    root = INDEXES[manifest["index"]][0]
    url = root + "/pypi/" + urllib.parse.quote(manifest["name"], safe="") + "/" + urllib.parse.quote(manifest["version"], safe="") + "/json"
    result = http_json(url, allow_missing=True)
    if result is None:
        return False
    info = result.get("info", {})
    if (canonical_name(info.get("name", "")) != manifest["name"] or info.get("version") != manifest["version"]):
        raise PolicyError("package index identity does not match")
    files = result.get("urls", [])
    matches = [item for item in files if item.get("filename") == manifest["filename"]]
    if len(matches) != 1 or matches[0].get("digests", {}).get("sha256") != manifest["sha256"] or matches[0].get("yanked", False):
        raise PolicyError("existing package filename/hash differs from retained artifact")
    return True


def upload(manifest_path, receipt_path, authorization_path=None, repo=None, artifact=None, policy_path=None, public_key=None):
    manifest = validate_manifest(load_json(manifest_path), artifact)
    if manifest["cicd_commit"] != git(Path(__file__).resolve().parent, "rev-parse", "HEAD"):
        raise PolicyError("upload helper does not match the prepared CICD revision")
    authorization = None
    if manifest["index"] == "pypi":
        if not authorization_path or not repo:
            raise PolicyError("production authorization is required before upload")
        authorization = load_json(authorization_path)
        policy_path = Path(policy_path) if policy_path else Path(__file__).with_name("release-policy.json")
        public_key = Path(public_key) if public_key else Path(repo) / "ci/release-signing-key.asc"
        if (file_hash(policy_path) != authorization.get("policy_sha256")
                or file_hash(public_key) != authorization.get("public_key_sha256")):
            raise PolicyError("authorized signing policy or public key changed")
        verify_tag(repo, authorization["tag"], load_json(policy_path), public_key)
        lock_path = Path(repo) / "ci/release-inputs.json"
        lock = committed_lock(repo, authorization["source_sha"], lock_path)
        if file_hash(lock_path) != authorization.get("input_lock_sha256"):
            raise PolicyError("authorized dependency lock changed")
        if lock.get("cicd_commit") != git(Path(__file__).resolve().parent, "rev-parse", "HEAD"):
            raise PolicyError("recovery helper does not match the locked CICD revision")
        validate_package_pins(manifest, lock)
        recheck(repo, authorization, manifest)
        # Prepared JSON is data, not an authorization credential. Re-query the
        # trusted control plane and public registries at the credential boundary.
        policy = load_json(policy_path)
        requirements = check_requirements(policy, lock, authorization["source_sha"])
        validate_checks(collect_cohort_checks(policy, lock, authorization["source_sha"]),
                        authorization["source_sha"], requirements)
        validate_input_lock(lock, authorization["tag"], inspect_inputs(lock))
    present = existing_package(manifest)
    if not present:
        username = os.environ.get("TWINE_USERNAME")
        password = os.environ.get("TWINE_PASSWORD")
        if username != "__token__" or not password or not password.startswith("pypi-"):
            raise PolicyError("selected package-index API credential is required")
        env = clean_environment()
        env.update({"TWINE_USERNAME": username, "TWINE_PASSWORD": password,
                    "TWINE_NON_INTERACTIVE": "1", "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        # Only the upload acknowledgement is recoverable. Authorization and
        # retained-byte failures must never be converted into successful receipts.
        with tempfile.TemporaryDirectory(prefix="release-upload-") as temporary:
            spool = Path(temporary) / manifest["filename"]
            shutil.copyfile(manifest["artifact"], spool)
            if file_hash(spool) != manifest["sha256"]:
                raise PolicyError("artifact changed before upload")
            spool.chmod(0o400)
            if authorization:
                recheck(repo, authorization, manifest)
            upload_error = None
            try:
                command([sys.executable, "-m", "twine", "upload", "--non-interactive",
                         "--repository-url", INDEXES[manifest["index"]][1], str(spool)],
                        env=env, timeout=180)
            except PolicyError as error:
                upload_error = error
            for attempt in range(3):
                if existing_package(manifest):
                    break
                if attempt == 2:
                    raise PolicyError("uploaded artifact could not be verified") from upload_error
                time.sleep(1)
    if authorization:
        recheck(repo, authorization, manifest)
    receipt = {"schema_version": 1, "package": manifest, "verified": True,
               "reused_existing": present, "verified_at": int(time.time()),
               "index": INDEXES[manifest["index"]][0],
               "source_sha": manifest["source_sha"], "cicd_commit": manifest["cicd_commit"]}
    if authorization:
        receipt.update({key: authorization[key] for key in ("tag", "tag_object", "source_sha", "cicd_commit", "input_lock_sha256")})
    write_json(receipt_path, receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    package = subs.add_parser("package-manifest")
    for option in ("artifact", "name", "version", "index", "source-sha", "cicd-commit", "output"):
        package.add_argument("--" + option, required=True)
    checks = subs.add_parser("collect-checks")
    for option in ("repository", "source-sha", "branch", "workflow", "output"):
        checks.add_argument("--" + option, required=True)
    checks.add_argument("--job", action="append", required=True)
    cohort = subs.add_parser("collect-cohort-checks")
    for option in ("policy", "lock", "source-sha", "output"):
        cohort.add_argument("--" + option, required=True)
    observe = subs.add_parser("inspect-inputs")
    observe.add_argument("--lock", required=True)
    observe.add_argument("--output", required=True)
    auth = subs.add_parser("authorize")
    for option in ("repo", "tag", "policy", "public-key", "lock", "checks", "observations", "artifact-manifest", "output"):
        auth.add_argument("--" + option, required=True)
    publish = subs.add_parser("upload")
    for option in ("manifest", "receipt"):
        publish.add_argument("--" + option, required=True)
    for option in ("authorization", "repo", "artifact", "policy", "public-key"):
        publish.add_argument("--" + option)
    args = parser.parse_args(argv)
    try:
        if args.command == "package-manifest":
            write_json(args.output, package_manifest(args.artifact, args.name, args.version, args.index,
                                                     args.source_sha, args.cicd_commit))
        elif args.command == "collect-checks":
            write_json(args.output, collect_checks(args.repository, args.source_sha, args.branch, args.workflow, args.job))
        elif args.command == "collect-cohort-checks":
            write_json(args.output, collect_cohort_checks(load_json(args.policy), load_json(args.lock), args.source_sha))
        elif args.command == "inspect-inputs":
            write_json(args.output, inspect_inputs(load_json(args.lock)))
        elif args.command == "authorize":
            write_json(args.output, authorize(args.repo, args.tag, args.policy, args.public_key, args.lock,
                                              args.checks, args.observations, args.artifact_manifest))
        elif args.command == "upload":
            upload(args.manifest, args.receipt, args.authorization, args.repo, args.artifact, args.policy, args.public_key)
    except (PolicyError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("release policy: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
