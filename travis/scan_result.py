#!/usr/bin/env python3
"""Bounded advisory scan collection with explicit unknown/error states.

Tool archives are pinned in scan-tools.lock.json, verified before extracting the
one regular binary, and never executed as installers. No status token is passed
to Docker, download tools, Syft, Grype or Trivy. Legacy Travis policy lives in its
existing shell entrypoints; this collector is used by the new GHA path only.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request

STATES = {"ok", "unavailable", "queued", "unsupported", "error"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SEVERITIES = {"Critical": "C", "High": "H", "Medium": "M", "Low": "L", "Unknown": "U", "Negligible": "U"}
DEFAULT_LOCK = Path(__file__).with_name("scan-tools.lock.json")


def environment():
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")
            and not re.search(r"TOKEN|PASSWORD|PASSWD|PSWD|(?:^|_)PASS(?:$|_)|SECRET|TAGGER|ASKPASS", key, re.I)}


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execute(args, timeout=180):
    return subprocess.run(args, env=environment(), text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, check=True).stdout


def observation(state, *, digest=None, scanner=None, report=None, counts=None, reason=None):
    if state not in STATES:
        raise ValueError("invalid scan state")
    result = {"state": state, "observed_at": stamp(), "freshness": {"state": "unknown"}}
    if digest:
        result["digest"] = digest
    if scanner:
        result["scanner"] = scanner
    if report:
        result["report"] = report
    if reason:
        result["reason"] = reason
    if state == "ok":
        if not counts or set(counts) != set("CHMLU") or any(type(value) is not int or value < 0 for value in counts.values()):
            raise ValueError("ok scan requires complete nonnegative counts")
        result["counts"] = counts
        result["last_success"] = {key: result[key] for key in ("digest", "observed_at", "scanner", "report", "counts") if key in result}
    return result


def parse_counts(raw, scanner, expected_digest):
    data = json.loads(raw)
    if not isinstance(data, dict) or not DIGEST.fullmatch(expected_digest or ""):
        raise ValueError("missing scan target digest")
    counts = dict.fromkeys("CHMLU", 0)
    if scanner == "grype":
        if not isinstance(data.get("matches"), list):
            raise ValueError("missing Grype matches array")
        target = data.get("source", {}).get("target", {})
        if not isinstance(target, dict) or target.get("imageID") != expected_digest:
            raise ValueError("Grype target digest mismatch")
        vulnerabilities = [item["vulnerability"]["severity"] for item in data["matches"]]
    elif scanner == "trivy":
        if data.get("SchemaVersion") != 2 or not isinstance(data.get("Results"), list):
            raise ValueError("missing Trivy result schema")
        if data.get("Metadata", {}).get("ImageID") != expected_digest:
            raise ValueError("Trivy target digest mismatch")
        vulnerabilities = []
        for result in data["Results"]:
            values = result.get("Vulnerabilities", [])
            if not isinstance(values, list):
                raise ValueError("invalid Trivy vulnerabilities")
            vulnerabilities.extend(item["Severity"].title() for item in values)
    else:
        raise ValueError("unsupported scanner schema")
    for value in vulnerabilities:
        if value not in SEVERITIES:
            raise ValueError("unknown vulnerability severity")
        counts[SEVERITIES[value]] += 1
    return counts


class Tools:
    def __init__(self, directory, lock=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = Path(lock or DEFAULT_LOCK)
        self.lock = json.loads(self.lock_path.read_text())
        if self.lock.get("schema_version") != 1:
            raise ValueError("unsupported tool lock")

    def get(self, name):
        item = self.lock["tools"][name]
        spec_platform = self.lock.get("platform")
        host_platform = "linux/amd64" if platform.system() == "Linux" and platform.machine() in ("x86_64", "amd64") else "unsupported"
        if spec_platform not in (host_platform, "any"):
            raise NotImplementedError("unsupported scanner platform")
        # Explicit local paths remain SHA-pinned; useful for preprovisioned tools
        # and offline tests, without a production environment bypass.
        if "path" in item:
            target = Path(item["path"])
            if target.is_symlink() or not target.is_file() or sha256(target) != item["sha256"]:
                raise ValueError("local tool hash mismatch")
            return str(target), item["version"]
        if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("missing pinned tool SHA-256")
        target_dir = self.directory / (name + "-" + item["version"])
        target = target_dir / name
        receipt = target_dir / "verified.json"
        if target.is_file() and not target.is_symlink() and receipt.is_file():
            saved = json.loads(receipt.read_text())
            if saved.get("archive_sha256") == item["sha256"] and saved.get("binary_sha256") == sha256(target):
                return str(target), item["version"]
        with tempfile.TemporaryDirectory(prefix="scan-download-", dir=self.directory) as temporary:
            archive = Path(temporary) / "tool.tar.gz"
            if not item["url"].startswith("https://github.com/"):
                raise ValueError("scanner archive must use official HTTPS release URL")
            with urllib.request.urlopen(item["url"], timeout=30) as source, archive.open("wb") as output:
                deadline = time.monotonic() + 120
                size = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if time.monotonic() > deadline or size > 150 * 1024 * 1024:
                        raise ValueError("scanner download exceeded time/size bound")
                    output.write(chunk)
            if sha256(archive) != item["sha256"]:
                raise ValueError("scanner archive hash mismatch")
            with tarfile.open(archive, "r:gz") as packed:
                member = packed.getmember(item["binary"])
                if not member.isfile() or member.size > 300 * 1024 * 1024:
                    raise ValueError("scanner binary archive member is unsafe")
                target_dir.mkdir(parents=True, exist_ok=True)
                with packed.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            target.chmod(0o755)
            receipt.write_text(json.dumps({"archive_sha256": item["sha256"], "binary_sha256": sha256(target)}))
        return str(target), item["version"]


def scan(tools, scanner, reference, digest, output, timeout=600):
    output = Path(output)
    try:
        binary, version = tools.get(scanner)
        args = [binary, "docker:" + reference, "-o", "json"] if scanner == "grype" else [
            binary, "image", "--scanners", "vuln", "--format", "json", "--timeout", str(timeout) + "s", reference]
        text = execute(args, timeout)
        output.write_text(text, encoding="utf-8")
        counts = parse_counts(text, scanner, digest)
        return observation("ok", digest=digest, scanner=scanner + "@" + version, report=output.name, counts=counts)
    except NotImplementedError:
        state, reason = "unsupported", "scanner-platform-unsupported"
    except subprocess.TimeoutExpired:
        state, reason = "error", "scanner-timeout"
    except subprocess.CalledProcessError:
        state, reason = "error", "scanner-execution-failed"
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        state, reason = "unavailable" if not output.exists() else "error", "scanner-install-or-report-invalid"
    if not output.exists():
        output.write_text(json.dumps({"state": state, "reason": reason}) + "\n")
    return observation(state, digest=digest, scanner=scanner, report=output.name, reason=reason)


def first_base(dockerfile, build_args=()):
    values = dict(item.split("=", 1) for item in build_args)
    def expand(value):
        def replace(match):
            name = match.group(1) or match.group(2)
            if name not in values:
                raise ValueError("unresolved Dockerfile ARG")
            return values[name]
        value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace, value)
        if "$" in value:
            raise ValueError("unsupported Dockerfile expansion")
        return value
    for line in Path(dockerfile).read_text().replace("\\\n", "").splitlines():
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        if fields[0].upper() == "ARG":
            name, separator, default = fields[1].partition("=")
            if name not in values and separator:
                values[name] = expand(default)
        elif fields[0].upper() == "FROM":
            candidates = [value for value in fields[1:] if not value.startswith("--")]
            reference = expand(candidates[0])
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", reference):
                raise ValueError("invalid Dockerfile base reference")
            return reference
    raise ValueError("Dockerfile has no base")


def capture_base(dockerfile, output, *, build_args=(), pull=False, pull_once=None, expected_digest=None):
    result = {"state": "unavailable", "reason": "actual-build-base-unknown", "observed_at": stamp()}
    try:
        reference = first_base(dockerfile, build_args)
        result["reference"] = reference
        result["dockerfile"] = str(dockerfile)
        cache = json.loads(Path(pull_once).read_text()) if pull_once and Path(pull_once).exists() else {}
        if (pull or pull_once) and reference not in cache:
            execute(["docker", "pull", "--platform", "linux/amd64", reference])
        image_id = execute(["docker", "image", "inspect", "--format", "{{.Id}}", reference]).strip()
        digests = json.loads(execute(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", reference]))
        repository = reference.split("@", 1)[0]
        if ":" in repository.rsplit("/", 1)[-1]:
            repository = repository.rsplit(":", 1)[0]
        resolved = next(value.split("@", 1)[1] for value in digests if value.split("@", 1)[0] == repository)
        if not DIGEST.fullmatch(image_id) or not DIGEST.fullmatch(resolved):
            raise ValueError("invalid base identity")
        if expected_digest and resolved != expected_digest:
            raise ValueError("base differs from expected dependency digest")
        if reference in cache and cache[reference] != {"image_id": image_id, "digest": resolved}:
            raise ValueError("captured local build base changed")
        result = {"state": "ok", "reference": reference, "image_id": image_id, "digest": resolved,
                  "observed_at": stamp(), "dockerfile": str(dockerfile), "captured_before_build": True}
        if pull_once:
            cache[reference] = {"image_id": image_id, "digest": resolved}
            cache_path = Path(pull_once)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError):
        pass
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, sort_keys=True) + "\n")
    return result


def collect(image_ref, base_provenance, output, *, lock=None, tools_dir=None, timeout=600):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    try:
        tools = Tools(tools_dir or output.parent.parent / "tools", lock)
    except (OSError, ValueError, KeyError):
        tools = None
    image_id = None
    try:
        image_id = execute(["docker", "image", "inspect", "--format", "{{.Id}}", image_ref]).strip()
        if not DIGEST.fullmatch(image_id):
            image_id = None
    except (OSError, subprocess.SubprocessError):
        pass
    result = {"image_id": image_id, "image": {}, "base": {}}
    base = {}
    if base_provenance:
        try:
            base = json.loads(Path(base_provenance).read_text())
        except (OSError, ValueError):
            pass
    for target, digest in (("image", image_id), ("base", base.get("image_id") if base.get("captured_before_build") else None)):
        report_name = "cve" if target == "image" else "cve-base"
        if not tools or not digest:
            observation_data = observation("unavailable", digest=digest, reason="actual-build-base-unknown" if target == "base" else "scanner-input-unavailable")
            (output / (report_name + ".txt")).write_text(json.dumps(observation_data) + "\n")
        else:
            observation_data = scan(tools, "grype", digest, digest, output / (report_name + ".json"), timeout)
            (output / (report_name + ".txt")).write_text(json.dumps(observation_data, indent=2) + "\n")
        if target == "base" and base:
            observation_data["provenance"] = base
            if base.get("digest"):
                observation_data["base_repository_digest"] = base["digest"]
        result[target] = observation_data
    try:
        if not tools or not image_id:
            raise ValueError("SBOM tool/input unavailable")
        binary, version = tools.get("syft")
        text = execute([binary, "docker:" + image_id, "-o", "spdx-json"], timeout)
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("packages"), list) or not data.get("spdxVersion"):
            raise ValueError("invalid SPDX SBOM")
        (output / "sbom.txt").write_text(text)
        result["sbom"] = {"state": "ok", "scanner": "syft@" + version, "digest": image_id}
    except (OSError, ValueError, KeyError, TypeError, NotImplementedError, subprocess.SubprocessError, tarfile.TarError):
        result["sbom"] = {"state": "unavailable", "reason": "sbom-install-or-execution-failed"}
        (output / "sbom.txt").write_text(json.dumps(result["sbom"]) + "\n")
    # Trivy is additional evidence; Grype owns the canonical legacy counts.
    result["trivy"] = scan(tools, "trivy", image_id, image_id, output / "trivy.json", timeout) if tools and image_id else observation("unavailable", reason="scanner-input-unavailable")
    (output / "scan.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture-base")
    capture.add_argument("--dockerfile", required=True)
    capture.add_argument("--output", required=True)
    capture.add_argument("--build-arg", action="append", default=[])
    capture.add_argument("--pull", action="store_true")
    capture.add_argument("--pull-once-file")
    capture.add_argument("--expected-digest")
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--image-ref", required=True)
    collect_parser.add_argument("--base-provenance")
    collect_parser.add_argument("--output", required=True)
    collect_parser.add_argument("--lock")
    collect_parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    if args.command == "capture-base":
        result = capture_base(args.dockerfile, args.output, build_args=args.build_arg, pull=args.pull,
                              pull_once=args.pull_once_file, expected_digest=args.expected_digest)
        if args.expected_digest and result["state"] != "ok":
            parser.exit(1, "Expected build-base digest was not verified\n")
    else:
        collect(args.image_ref, args.base_provenance, args.output, lock=args.lock, timeout=args.timeout)


if __name__ == "__main__":
    main()
