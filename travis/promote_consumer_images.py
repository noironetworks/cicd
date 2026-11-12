#!/usr/bin/env python3
"""Promote authorized immutable manifests to previously absent consumer tags.

No build, mutable source alias, token argument, or existing-tag replacement is
allowed. The release-policy authorization boundary runs before credential files
are opened. All locked sources, dependencies and current destinations preflight
before any copy. A retained receipt supports forward resume after partial work.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import release_policy as policy

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REGISTRIES = {"quay.io/noiro", "docker.io/noiro"}
PUBLIC = {"registry.access.redhat.com", "registry.redhat.io"}


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def persist(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def image_repository(reference):
    if "@" in reference:
        repository, selected = reference.rsplit("@", 1)
        if not DIGEST.fullmatch(selected):
            raise policy.PolicyError("invalid immutable image reference")
    else:
        if not re.fullmatch(r".+:[A-Za-z0-9][A-Za-z0-9._-]*", reference):
            raise policy.PolicyError("noncanonical image reference")
        repository = reference.rsplit(":", 1)[0]
    if not re.fullmatch(r"(?:quay\.io|docker\.io|registry\.access\.redhat\.com|registry\.redhat\.io)/[A-Za-z0-9_./-]+", repository):
        raise policy.PolicyError("noncanonical image repository")
    if any(part in ("", ".", "..") for part in repository.split("/")):
        raise policy.PolicyError("invalid image repository path")
    return repository


class Promotion:
    def __init__(self, args, identity, lock):
        self.args, self.identity, self.lock = args, identity, lock
        self.path = Path(args.receipt)
        if self.path.is_symlink():
            raise policy.PolicyError("receipt symlink is prohibited")
        binding = {**identity, "cicd_commit": lock["cicd_commit"],
                   "input_lock_sha256": hashlib.sha256(Path(args.lock).read_bytes()).hexdigest()}
        if self.path.exists():
            self.receipt = json.loads(self.path.read_text())
            if self.receipt.get("schema_version") != 1 or self.receipt.get("authorization") != binding:
                raise policy.PolicyError("retained receipt belongs to different authorized inputs")
        else:
            self.receipt = {"schema_version": 1, "authorization": binding, "state": "preflight",
                            "operations": [], "results": {}, "started_at": stamp()}
        self.authfiles = {"quay.io": self.authfile(args.quay_authfile, "quay.io"),
                          "docker.io": self.authfile(args.docker_authfile, "docker.io")}
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("GIT_CONFIG_") and key not in ("DOCKER_CONFIG", "REGISTRY_AUTH_FILE")
                            and not re.search(r"TOKEN|PASSWORD|PASSWD|PSWD|(?:^|_)PASS(?:$|_)|SECRET|TAGGER|ASKPASS", key, re.I)}
        self.save()

    @staticmethod
    def authfile(value, host):
        path = Path(value)
        if path.is_symlink() or not path.is_file():
            raise policy.PolicyError("explicit scoped registry authfile is required")
        auths = json.loads(path.read_text()).get("auths")
        if not isinstance(auths, dict) or not auths:
            raise policy.PolicyError("registry authfile must contain explicit scoped auths")
        allowed = (host, "https://index.docker.io/v1/") if host == "docker.io" else (host,)
        if any(not any(key == item or key.startswith(item + "/") for item in allowed) for key in auths):
            raise policy.PolicyError("authfile contains credentials for another registry")
        return str(path.resolve())

    def save(self):
        persist(self.path, self.receipt)

    def auth(self, reference):
        host = reference.split("/", 1)[0]
        return ["--authfile", self.authfiles[host]] if host in self.authfiles else ["--no-creds"]

    def operation(self, phase, reference, arguments, *, missing=False):
        event = {"phase": phase, "reference": reference, "state": "started", "started_at": stamp()}
        self.receipt["operations"].append(event)
        self.save()
        try:
            result = subprocess.run([self.args.skopeo, *arguments], env=self.environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.args.timeout)
            if result.returncode:
                reason = result.stderr.decode("utf-8", "replace").lower()
                if missing and any(value in reason for value in ("manifest unknown", "manifest_unknown", "name unknown", "name_unknown", "manifest does not exist")):
                    event["state"] = "missing"
                    return None
                raise policy.PolicyError("Skopeo operation failed")
            event["state"] = "ok"
            return result.stdout
        except (OSError, subprocess.TimeoutExpired, policy.PolicyError) as error:
            event.update(state="error", reason="timeout" if isinstance(error, subprocess.TimeoutExpired) else "operation-failed")
            raise policy.PolicyError("bounded registry operation failed") from error
        finally:
            event["finished_at"] = stamp()
            self.save()

    def raw(self, reference, *, missing=False, phase="inspect-manifest"):
        return self.operation(phase, reference, ["inspect", "--raw", *self.auth(reference), "docker://" + reference], missing=missing)

    def observe(self, reference, expected, *, missing=False):
        raw = self.raw(reference, missing=missing)
        if raw is None:
            return None
        observed = digest(raw)
        if observed != expected:
            self.receipt["operations"][-1].update(state="error", reason="manifest-digest-mismatch",
                                                  expected_digest=expected, observed_digest=observed)
            self.save()
            raise policy.PolicyError("existing manifest differs from the approved digest")
        manifest = json.loads(raw)
        repository = reference.split("@", 1)[0] if "@" in reference else image_repository(reference)
        if isinstance(manifest.get("manifests"), list):
            choices = [item for item in manifest["manifests"] if item.get("platform", {}).get("os") == "linux"
                       and item.get("platform", {}).get("architecture") == "amd64"]
            if len(choices) != 1 or not DIGEST.fullmatch(choices[0].get("digest", "")):
                raise policy.PolicyError("manifest index has no unambiguous linux/amd64 image")
            child = self.raw(repository + "@" + choices[0]["digest"])
            if digest(child) != choices[0]["digest"]:
                raise policy.PolicyError("platform manifest digest mismatch")
            manifest = json.loads(child)
            reference = repository + "@" + choices[0]["digest"]
        config_digest = manifest.get("config", {}).get("digest", "")
        if manifest.get("schemaVersion") != 2 or not DIGEST.fullmatch(config_digest):
            raise policy.PolicyError("unsupported image manifest/config")
        config_raw = self.operation("inspect-config", reference,
                                    ["inspect", "--raw", "--config", *self.auth(reference), "docker://" + reference])
        config = json.loads(config_raw)
        if digest(config_raw) != config_digest or config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise policy.PolicyError("image config digest/platform mismatch")
        return observed

    def run(self):
        targets, dependencies = [], []
        for name, image in self.lock["images"].items():
            for registry, item in image["registries"].items():
                reference, expected = item["reference"], item["digest"]
                repository = image_repository(reference)
                if registry not in REGISTRIES or repository != registry + "/" + name or reference.endswith(".z") or not DIGEST.fullmatch(expected):
                    raise policy.PolicyError("invalid consumer target")
                targets.append((reference, repository + "@" + expected, expected))
        for item in self.lock["dependencies"]:
            repository = image_repository(item["reference"])
            if not DIGEST.fullmatch(item["digest"]):
                raise policy.PolicyError("invalid locked base input")
            dependencies.append((repository + "@" + item["digest"], item["digest"]))
        # Every immutable input and every destination must be qualified first.
        try:
            for reference, expected in dependencies + [(source, expected) for _, source, expected in targets]:
                self.observe(reference, expected)
            existing = {target: self.observe(target, expected, missing=True) for target, _, expected in targets}
            self.receipt["state"] = "promoting"
            self.save()
            for target, source, expected in targets:
                if existing[target] == expected:
                    self.receipt["results"][target] = {"state": "already-present", "digest": expected}
                    self.save()
                    continue
                # Close the preflight/write window for authorized source identity
                # and for a concurrent publisher claiming this immutable tag.
                if self.observe(target, expected, missing=True) == expected:
                    self.receipt["results"][target] = {"state": "already-present", "digest": expected}
                    self.save()
                    continue
                host = target.split("/", 1)[0]
                arguments = ["copy", "--all", "--preserve-digests", "--src-authfile", self.authfiles[host],
                             "--dest-authfile", self.authfiles[host], "docker://" + source, "docker://" + target]
                policy.assert_remote_tag(self.args.repository, self.identity)
                lost_ack = False
                try:
                    self.operation("copy", target, arguments)
                except policy.PolicyError:
                    lost_ack = True
                if self.observe(target, expected, missing=True) != expected:
                    raise policy.PolicyError("copy did not produce the approved target manifest")
                self.receipt["results"][target] = {"state": "verified-after-ack-loss" if lost_ack else "copied",
                                                    "digest": expected, "source": source, "verified_at": stamp()}
                self.save()
            self.receipt.update(state="complete", finished_at=stamp())
            self.save()
        except (policy.PolicyError, OSError, ValueError, KeyError, TypeError) as error:
            self.receipt.update(state="partial" if self.receipt["results"] else "failed", failure=str(error), finished_at=stamp())
            self.save()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repository", "tag", "policy", "public-key", "lock", "checks", "receipt", "quay-authfile", "docker-authfile"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--skopeo", default="skopeo")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    promotion = None
    try:
        identity, lock = policy.authorize_source(args.repository, args.tag, args.policy, args.public_key, args.lock, args.checks)
        # This map validates signed lock shape/cohort only. Actual readiness is
        # established by immutable-source preflight, before any target exists.
        locked = {item["reference"]: item["digest"] for image in lock["images"].values()
                  for item in image["registries"].values()}
        locked.update({item["reference"]: item["digest"] for item in lock["dependencies"]})
        policy.validate_input_lock(lock, args.tag, {"schema_version": 1, "digests": locked,
                                                   "platforms": dict.fromkeys(locked, "linux/amd64")})
        policy.assert_remote_tag(args.repository, identity)
        promotion = Promotion(args, identity, lock)
        promotion.run()
        return 0
    except (policy.PolicyError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        if promotion:
            promotion.receipt.update(state="partial" if promotion.receipt["results"] else "failed",
                                     failure=str(error), finished_at=stamp())
            promotion.save()
        print("Promotion error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
